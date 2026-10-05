#!/usr/bin/env python3
"""
系統健康檢查腳本

每日執行，檢查：
1. JWT Token 有效期
2. 磁碟空間
3. 最近一次同步狀態
4. API 可用性

異常時發送通知。

用法：
  python3 health_check.py          # 檢查並顯示結果
  python3 health_check.py --notify  # 異常時發送通知
"""

import os
import sys
import json
import time
import argparse
import shutil
from datetime import datetime
from pathlib import Path

# 載入設定
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()


def check_token():
    """檢查 JWT Token 有效期"""
    from geobingan_sync.jwt_auth import decode_jwt_payload
    refresh = os.getenv('REFRESH_TOKEN', '')
    if not refresh:
        return 'error', 'REFRESH_TOKEN 未設定'

    try:
        payload = decode_jwt_payload(refresh)
        exp = payload.get('exp', 0)
        days_left = (exp - time.time()) / 86400
        # Thresholds 假設 refresh_token lifetime = 7 天（riskmap.tw server-side）
        # 5 天 warning 給 ~5d buffer；<= 1 天升 urgent error；超 7 天的 lifetime 變更要同步調這裡
        if days_left < 0:
            return 'error', f'Refresh Token 已過期 {-days_left:.1f} 天，請立即到 riskmap.tw 重新登入取得新 refresh_token'
        elif days_left <= 1:
            return 'error', f'Refresh Token 即將過期 < {days_left*24:.0f} 小時，請立即到 riskmap.tw 重新登入取得新 refresh_token'
        elif days_left < 5:
            return 'warning', f'Refresh Token 剩餘 {days_left:.1f} 天，請盡快到 riskmap.tw 重新登入取得新 refresh_token'
        else:
            return 'ok', f'Refresh Token 剩餘 {days_left:.1f} 天'
    except Exception as e:
        return 'error', f'Token 檢查失敗: {e}'


def check_pause():
    """偵測 .pause_upload 旗標並提醒暫停時長（stale-pause guard，呼應 #57 silent lock）。

    ≥30 天升級 error：python3 -m geobingan_sync.steps.upload_pdfs 用 30 天檔名日期窗，暫停超過此窗、恢復時
    落窗外的報告會被靜默漏掉，須 `python3 -m geobingan_sync.steps.upload_pdfs --catchup-days N` 補掃。
    """
    flag = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.pause_upload')
    if not os.path.exists(flag):
        return 'ok', '上傳未暫停'
    days = (time.time() - os.path.getmtime(flag)) / 86400
    base = f'上傳已暫停 {days:.1f} 天（.pause_upload；恢復：rm .pause_upload）'
    if days >= 30:
        return 'error', base + '；⚠️ 已超過 30 天檔名窗，恢復時須 python3 -m geobingan_sync.steps.upload_pdfs --catchup-days N 補掃避免漏報告'
    return 'warning', base


def check_disk():
    """檢查磁碟空間"""
    usage = shutil.disk_usage('/')
    free_gb = usage.free / (1024**3)
    pct_used = usage.used / usage.total * 100
    if free_gb < 5:
        return 'warning', f'磁碟空間不足: {free_gb:.1f} GB 可用 ({pct_used:.0f}% 已用)'
    return 'ok', f'{free_gb:.1f} GB 可用'


#: 模組常數而非寫死字串，測試才擋得住（否則檢查結果會取決於本機有沒有這個檔）
SYNC_STATUS_FILE = './state/sync_status.json'
SYNC_STALE_DAYS = 10              # check_last_sync 超過這個天數才報同步停擺


def check_last_sync(path=None, now=None):
    """檢查最近一次同步狀態。

    path／now 可注入：check_unsupported_sources 會**直接呼叫這支**來判斷「同步
    停擺是否已經有人報了」，不自己複製門檻（見那支的說明）。
    """
    status_file = path or SYNC_STATUS_FILE
    if not os.path.exists(status_file):
        return 'warning', '找不到同步狀態檔案'

    try:
        with open(status_file, 'r') as f:
            data = json.load(f)
        last_run = data.get('last_run', '')
        last_status = data.get('last_status', '')
        if not last_run:
            return 'warning', '尚未執行過同步'

        days_ago = ((now or datetime.now()) - datetime.fromisoformat(last_run)).days
        if days_ago > SYNC_STALE_DAYS:
            return 'warning', f'距離上次同步已 {days_ago} 天（{last_run[:10]}）'
        elif last_status == 'failure':
            return 'warning', f'上次同步失敗（{last_run[:10]}）'
        return 'ok', f'上次同步: {last_run[:10]}（{last_status}）'
    except Exception as e:
        return 'warning', f'讀取狀態失敗: {e}'


def check_api():
    """檢查 API 可用性"""
    import requests
    try:
        r = requests.get('https://riskmap.today/api/', timeout=10)
        if r.status_code < 500:
            return 'ok', f'API 正常（{r.status_code}）'
        return 'warning', f'API 回應異常（{r.status_code}）'
    except Exception as e:
        return 'error', f'API 無法連線: {e}'


STALE_HOURS = 6
ANCIENT_HOURS = 24 * 7   # 超過 7 天視為陳年積壓，不驅動燈號
MAX_PAGES = 5            # 每種狀態最多掃 5 頁 × 200


def _api_token():
    """取得可用 JWT（過期則用 refresh token 換發，並寫回 .env，與 match_permits 相同作法）。"""
    from geobingan_sync.jwt_auth import get_valid_token
    from geobingan_sync import config
    token, was_refreshed, new_refresh = get_valid_token(config.JWT_TOKEN, config.REFRESH_TOKEN,
                                                        config.GEOBINGAN_REFRESH_URL)
    if was_refreshed and token:
        try:
            config.update_jwt_token(token, new_refresh)
        except Exception:
            pass
    return token


def count_stale_reports(pages, now, stale_hours=STALE_HOURS, ancient_hours=ANCIENT_HOURS):
    """純函式：pages 為 API 回傳的 results 列表們。

    回 dict：total、recent（stale_hours ≤ 年齡 < ancient_hours，該跑完卻沒跑＝新事故訊號）、
    ancient（≥ ancient_hours 的陳年積壓，只註記不驅動燈號，否則舊帳會讓告警永遠紅燈、
    蓋掉新事故）、recent_oldest_h。
    """
    from datetime import timezone
    out = {'total': 0, 'recent': 0, 'ancient': 0, 'recent_oldest_h': 0.0}
    for results in pages:
        for r in results:
            out['total'] += 1
            ts = r.get('updated_at') or r.get('created_at')
            if not ts:
                continue
            try:
                t = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            except ValueError:
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            hours = (now - t).total_seconds() / 3600
            if hours >= ancient_hours:
                out['ancient'] += 1
            elif hours >= stale_hours:
                out['recent'] += 1
                out['recent_oldest_h'] = max(out['recent_oldest_h'], hours)
    return out


def check_parse_backlog(now=None):
    """後端解析積壓：近 7 天內卡住 ≥6h 的報告 → 告警（對應 9/14 事故：預算爆掉後 116 份卡一整天沒人知）。

    燈號只看「近期停滯」：error＝≥20 份或最舊 ≥24h；warning＝1–19 份；ok＝0。
    陳年積壓（>7 天）只在訊息註記，避免舊帳讓告警永遠紅燈、蓋掉新事故。
    掃描上限 MAX_PAGES×200，達上限標「≥」。API/認證失敗只回 warning（token 到期另有 check_token）。
    """
    import requests
    from datetime import timezone
    from geobingan_sync.config import GEOBINGAN_BASE_URL
    try:
        token = _api_token()
        if not token:
            return 'warning', '解析積壓檢查略過：無可用 JWT'
        base = GEOBINGAN_BASE_URL.rstrip('/')
        h = {'Authorization': f'Bearer {token}'}
        pages, capped = [], False
        for status in ('pending', 'processing'):
            url = f'{base}/api/reports/construction-reports/?parse_status={status}&page_size=200'
            for i in range(MAX_PAGES):
                resp = requests.get(url, headers=h, timeout=20)
                resp.raise_for_status()                      # 401/500 不可變成假綠燈（review P2）
                d = resp.json()
                if not isinstance(d, dict) or not isinstance(d.get('results'), list):
                    raise ValueError(f'backlog 回應格式異常: {str(d)[:120]}')
                pages.append(d['results'])
                url = d.get('next')
                if not url:
                    break
                if i == MAX_PAGES - 1:
                    capped = True
        now = now or datetime.now(timezone.utc)
        c = count_stale_reports(pages, now)
        ge = '≥' if capped else ''
        note = f'；另有陳年積壓 {ge}{c["ancient"]} 份（>{ANCIENT_HOURS // 24} 天，未計入燈號）' if c['ancient'] else ''
        if c['recent'] == 0:
            return 'ok', f'近期解析佇列正常（未完成共 {ge}{c["total"]}{note}）'
        msg = (f'{c["recent"]} 份近期解析停滯 ≥{STALE_HOURS}h（最舊 {c["recent_oldest_h"]:.0f}h）'
               f'— 疑 worker 停擺或 OpenAI 預算上限，請查 PROD-348 類狀況{note}')
        level = 'error' if (c['recent'] >= 20 or c['recent_oldest_h'] >= 24) else 'warning'
        return level, msg
    except Exception as e:
        return 'warning', f'解析積壓檢查失敗: {e}'


def check_budget(path=None):
    """今日解析估算成本 vs DAILY_BUDGET_USD：≥70% warning、≥90% error。path 可注入供測試。

    今日消耗＝上傳＋手動 retry-parse（兩者吃同一份後端日額度）。
    """
    from geobingan_sync.budget import DailyBudget, budget_level
    from geobingan_sync.config import COST_PER_REPORT_USD, DAILY_BUDGET_USD
    try:
        m = DailyBudget(path=path, cost_per_report=COST_PER_REPORT_USD).load()
        level, ratio = budget_level(float(m.get('est_usd', 0)), DAILY_BUDGET_USD)
        msg = (f"今日({m['day']}) 上傳 {m['uploaded']}＋重推 {m['retried']} ＝ {m['units']} 份 "
               f"≈ US${float(m.get('est_usd', 0)):.2f} / 日上限 US${DAILY_BUDGET_USD:.0f}（{ratio:.0%}）")
        if level != 'ok':
            msg += '；今日額度將盡，上傳會被自動裁切，明日重置'
        return level, msg
    except Exception as e:
        return 'warning', f'預算檢查失敗: {e}'


DEATH_WINDOW_DAYS = 7


def check_list_freshness(path=None, now=None):
    """政府清單新鮮度：動態解析失效（退回靜態舊清單）→ error；長期未更新 → warning。

    PR #78 的 fallback 只印 log，沒有告警——建管處改版導致解析失效時，我們會
    靜默同步過期清單（原本 8 個月沒發現的情境）。path/now 可注入供測試。
    """
    from geobingan_sync.list_fingerprint import ListFingerprint, assess
    try:
        return assess(ListFingerprint(path=path).load(), now=now)
    except Exception as e:
        return 'warning', f'清單指紋檢查失敗: {e}'


def check_folder_deaths(path=None, now=None, window_days=DEATH_WINDOW_DAYS):
    """來源資料夾由活轉死：近 N 天內新發生的失效 → error（資料流失訊號）。

    對應 111建字第0311號：253 份監測報告的來源資料夾被刪、數月後才發現。
    舊的失效只留在紀錄裡、不驅動燈號，避免舊帳讓告警永遠紅燈。
    """
    from datetime import datetime as _dt, timedelta
    from geobingan_sync.steps.match_permits import FOLDER_DEATHS_FILE
    try:
        p = path or FOLDER_DEATHS_FILE
        try:
            with open(p, encoding='utf-8') as f:
                log = json.load(f)
        except FileNotFoundError:
            return 'ok', '無來源資料夾失效紀錄'
        except json.JSONDecodeError as e:
            # 損毀不可當成「無紀錄」的綠燈——那正是這個檢查要防的無聲流失
            return 'warning', f'失效紀錄檔損毀、無法判讀（{e}）；請檢查 state/folder_deaths.json'
        now = now or _dt.now()
        cutoff = (now - timedelta(days=window_days)).strftime('%Y-%m-%d')
        recent = [d for d in (log.get('deaths') or []) if str(d.get('detected', '')) >= cutoff]
        if not recent:
            total = len(log.get('deaths') or [])
            return 'ok', f'近 {window_days} 天無新增失效（歷史累計 {total} 個）'
        pdfs = sum(int(d.get('pdf_count') or 0) for d in recent)
        names = '、'.join(f"{d.get('permit')}" for d in recent[:3])
        return 'error', (f'{len(recent)} 個來源資料夾近 {window_days} 天內失效（影響約 {pdfs} 份 PDF）：'
                         f'{names}{"…" if len(recent) > 3 else ""}——請向監測公司/建管處確認去向')
    except Exception as e:
        return 'warning', f'失效資料夾檢查失敗: {e}'


UNSUPPORTED_STALE_HOURS = 48      # 每日排程，容忍漏跑一次；再久就是名單本身停更了


def _last_sync_time(path=None):
    """最近一次同步時間；讀不到就回 None（代表無從判斷，不是「沒跑過」）。"""
    from datetime import datetime as _dt
    try:
        with open(path or SYNC_STATUS_FILE, encoding='utf-8') as f:
            raw = (json.load(f) or {}).get('last_run') or ''
        return _dt.fromisoformat(raw) if raw else None
    except Exception:                                   # noqa: BLE001
        return None


def check_unsupported_sources(path=None, now=None, new_window_days=14,
                              sync_status_path=None,
                              stale_hours=UNSUPPORTED_STALE_HOURS):
    """清單上有、但我們接不到的來源。

    這個檢查不是「系統壞了」的燈號，是**缺口的可見度**——被剔除的建案原本在系統
    裡完全消失。平時綠燈並附上家族分佈；基準建立後**新出現**的才升警告，因為那
    代表某個原本接得到的來源剛剛失聯，是新的資料流失訊號。

    家族結論帶探測日期一起顯示：那是點時間的觀察，不是永久事實。

    ⚠️ 也要檢查**這個機制本身還活著**（review P2）。名單寫入失敗刻意不中斷同步
    （它是可見度、不是同步本身），代價是失敗會無聲：舊檔會一直回報「無新增」綠燈，
    第一次就寫不出來則會永遠回報「尚未跑過」綠燈。所以同步跑過之後缺檔、或
    generated_at 過期，都要升警告——否則正是這支功能要消滅的那種無聲失效。
    """
    from datetime import datetime as _dt, timedelta
    from geobingan_sync import unsupported_sources as us
    try:
        now = now or _dt.now()
        last_sync = _last_sync_time(sync_status_path)
        try:
            data = us.load(path)
        except json.JSONDecodeError as e:
            return 'warning', f'未支援來源名單損毀、無法判讀（{e}）；請檢查 state/unsupported_sources.json'
        if not data:
            if last_sync is None:
                return 'ok', '尚無未支援來源名單（同步尚未跑過）'
            sync_level, sync_msg = check_last_sync(path=sync_status_path, now=now)
            if sync_level != 'ok':
                # 只有在「同步狀態」檢查**確實會亮燈**時才讓給它報。
                # 早先版本用自己的 48 小時門檻抑制告警，但 check_last_sync 要超過
                # 10 天才報 → 最後一次成功同步在 3～10 天前而名單缺失時，兩邊
                # 同時綠燈，出現告警真空（review P2）。門檻不複製，直接問它。
                return 'ok', f'尚無未支援來源名單（同步狀態已另行告警：{sync_msg}）'
            return 'warning', (f'同步已於 {last_sync:%Y-%m-%d %H:%M} 執行，卻找不到未支援來源名單'
                               f'——名單寫入可能一直失敗，缺口目前沒有任何紀錄')
        gen_raw = data.get('generated_at') or ''
        try:
            gen = _dt.fromisoformat(gen_raw)
        except (TypeError, ValueError):
            return 'warning', f'未支援來源名單缺少可判讀的 generated_at（{gen_raw!r}）——無法確認是否仍在更新'
        age = now - gen
        if age > timedelta(hours=stale_hours):
            hrs = int(age.total_seconds() // 3600)
            return 'warning', (f'未支援來源名單已 {hrs} 小時未更新（最後 {gen:%Y-%m-%d %H:%M}）'
                               f'——寫入可能持續失敗，名單內容已不可信')
        sources = data.get('sources') or {}
        if not sources:
            return 'ok', '清單上的來源全部接得到'
        cutoff = (now - timedelta(days=new_window_days)).strftime('%Y-%m-%d')
        # 「新增」= 基準建立**之後**才出現，且在觀察窗內。
        # 只看 first_seen 是不夠的：基準剛建立那幾天所有 first_seen 都很新，
        # 整份名單都會被算成新事故。
        baseline = str(data.get('baseline_since') or '')
        fresh = ([] if data.get('first_run') else
                 [p for p, i in sources.items()
                  if baseline and str(i.get('first_seen', '')) > baseline
                  and str(i.get('first_seen', '')) >= cutoff])
        rows = us.summarise(data)
        brief = '；'.join(f'{fam} {n}（{status}，{date}{"" if date == "未探測" else " 探測"}）'
                          for fam, n, status, date, _ in rows[:4])
        if fresh:
            names = '、'.join(sorted(fresh)[:3])
            return 'warning', (f'{len(sources)} 案來源接不到，其中 {len(fresh)} 案為近 '
                               f'{new_window_days} 天新增：{names}{"…" if len(fresh) > 3 else ""}'
                               f'——新失聯要確認去向。{brief}')
        if data.get('first_run'):
            return 'ok', f'{len(sources)} 案來源接不到（首輪建立基準，下輪起才判定新增）。{brief}'
        return 'ok', f'{len(sources)} 案來源接不到（近 {new_window_days} 天無新增）。{brief}'
    except Exception as e:
        return 'warning', f'未支援來源檢查失敗: {e}'


#: 同步進度與錯誤紀錄。刻意不 import sync_permits 取它的 STATE_FILE——那會把
#: google-api 整套拉進健康檢查。兩邊各寫一份有漂走的風險，所以用測試釘住相等。
SYNC_PROGRESS_FILE = './state/sync_permits_progress.json'

#: 每日排程，容忍漏跑一次；再久就代表結算本身沒在跑了
SYNC_ERROR_STALE_HOURS = 48


def check_sync_errors(path=None, now=None, sync_status_path=None,
                      stale_hours=SYNC_ERROR_STALE_HOURS):
    """同步時逐案失敗的可見度。

    為什麼需要：2026-10-05 一輪出現 10 筆 Connection reset／讀取逾時，exit code
    照樣 0、沒有任何人告警，是手動 grep 日誌才看到的。而且當時 errors 項目**沒有
    時間戳**（實測 2,067 筆），連「今天有沒有出錯」都答不出來。

    兩個獨立訊號，不混成一個門檻：
      · 系統性故障——單輪錯誤佔走訪案數比例過高（憑證失效、網路斷）
      · 個案卡死——同一案連續 N 輪失敗才算有定論；單次抖動會同時打中執行緒池
        的 5–10 案，那不是個案壞了（10/05 的 10/452 = 2.2% 刻意不該亮）

    ⚠️ 未結算 ≠ 零錯誤。沒有 last_run 或它過期，一律回報「無從判斷」而不是綠燈
    ——10/05 我自己就把「同步還在跑所以還沒寫」讀成了「量到 0」。
    """
    from datetime import datetime as _dt, timedelta
    from geobingan_sync import sync_errors
    try:
        now = now or _dt.now()
        try:
            with open(path or SYNC_PROGRESS_FILE, encoding='utf-8') as f:
                state = json.load(f) or {}
        except FileNotFoundError:
            return 'ok', '尚無同步進度紀錄（同步尚未跑過）'
        except json.JSONDecodeError as e:
            return 'warning', f'同步進度紀錄損毀、無法判讀（{e}）；請檢查 state/sync_permits_progress.json'

        res = sync_errors.summarise_last_run(state)
        stuck = res['stuck']

        if not res['known']:
            # 「還沒結算」不可當綠燈。但若同步本身已經在告警，就讓給它報，
            # 不要兩支都亮同一件事（門檻不複製，直接問它）。
            sync_level, sync_msg = check_last_sync(path=sync_status_path, now=now)
            if sync_level != 'ok':
                return 'ok', f'同步錯誤尚未結算（同步狀態已另行告警：{sync_msg}）'
            return 'warning', ('同步錯誤尚未結算——結算步驟可能一直失敗，'
                               '本輪有多少案出錯目前無從判斷')

        try:
            at = _dt.fromisoformat(str(res['at']))
        except (TypeError, ValueError):
            return 'warning', f"同步錯誤結算缺少可判讀的時間（{res['at']!r}）——無法確認是否仍在更新"
        age = now - at
        if age > timedelta(hours=stale_hours):
            hrs = int(age.total_seconds() // 3600)
            sync_level, sync_msg = check_last_sync(path=sync_status_path, now=now)
            if sync_level != 'ok':
                return 'ok', f'同步錯誤結算已 {hrs} 小時未更新（同步狀態已另行告警：{sync_msg}）'
            return 'warning', (f'同步錯誤結算已 {hrs} 小時未更新（最後 {at:%Y-%m-%d %H:%M}）'
                               f'——結算可能持續失敗，錯誤數已不可信')

        base = f"最近一輪 {res['errored']} / {res['visited']} 案出錯（{res['rate']:.1%}）"
        if res['systemic']:
            return 'error', (f"{base}，超過 {sync_errors.RUN_ERROR_RATE_ALERT:.0%} 門檻"
                             f"——像是系統性故障（憑證／網路），不是個案抖動")
        if stuck:
            names = '、'.join(f'{p}({n} 輪)' for p, n in stuck[:3])
            return 'error', (f"{len(stuck)} 案連續 {sync_errors.ERROR_STREAK_ALERT} 輪以上失敗："
                             f"{names}{'…' if len(stuck) > 3 else ''}"
                             f"——已非暫時性，要逐案查。{base}")
        if res['errored']:
            return 'ok', (f"{base}；無連續失敗達 {sync_errors.ERROR_STREAK_ALERT} 輪的案"
                           f"——研判為暫時性，下輪會自然重試")
        return 'ok', f"最近一輪 {res['visited']} 案全部無錯誤"
    except Exception as e:
        return 'warning', f'同步錯誤檢查失敗: {e}'


def check_launchd_jobs():
    """檢查所有 launchd 排程 job 的最後執行狀態（清單見 jobs；新增 plist 時必須同步加入，否則鎖死不會被巡到）。

    動機：launchd 對 EX_CONFIG 等錯誤會 silent backoff 鎖死整個 schedule、
    沒有任何 alerting。fridayreport 5/1 鎖 3 週、weeklysync 4 月某次鎖 4 週
    都是下游發現「咦週報沒進來」才察覺。把巡檢納入每日健檢、stuck 立刻浮現。

    註：用 `gui/{uid}` domain 查 LaunchAgent，需在 GUI (Aqua) session 執行。
    純 SSH session（無 GUI）可能查不到、會回報「無法解析」— 屬預期保守誤報。
    """
    import subprocess
    import re
    # 與 launchd/*.plist 一一對應。值＝「這個 job 的正常結束碼」：
    # drainstuck 的 EXIT_PARSER_HELD（5）是**設計上的正常結果**（探測到解析引擎異常
    # → 今天不放行），不是 launchd backoff 鎖死；不列進來每天都會誤報一次，
    # 而告警被雜訊稀釋正是這套巡檢要防的事。
    # 6（EXIT_PAUSED）＝人為暫停（.pause_upload），同樣是設計結果。
    # **exit 4 不可列入**：那是 retry_parse 查詢失敗／狀態未知，列進去會把
    # API／token 故障吞掉變 silent failure（review P1）。
    from geobingan_sync.parser_health import EXIT_PARSER_HELD
    from geobingan_sync.steps.drain_stuck import EXIT_PAUSED
    jobs = {'healthcheck': {0}, 'weeklysync': {0}, 'fridayreport': {0},
            'drainstuck': {0, EXIT_PARSER_HELD, EXIT_PAUSED}}
    uid = os.getuid()
    bad = []
    for job, okay_codes in jobs.items():
        label = f'com.geothings.geobingan.{job}'
        try:
            out = subprocess.run(
                ['launchctl', 'print', f'gui/{uid}/{label}'],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except Exception:
            bad.append(f'{job}（查詢失敗）')
            continue
        if not out:
            bad.append(f'{job}（未載入）')
            continue
        # launchctl 輸出形如 "last exit code = 78: EX_CONFIG" 或 "= 0" 或 "= (never exited)"
        m = re.search(r'last exit code = (\d+|\(never)', out)
        # '(never' = 從未跑過（剛 reload，正常）；'0' = 正常；其他非零 = 異常
        if m is None:
            bad.append(f'{job}（無法解析 launchctl 輸出、請手動確認）')
        elif m.group(1) != '(never' and int(m.group(1)) not in okay_codes:
            bad.append(f'{job}（last exit={m.group(1)}）')
    if bad:
        return 'warning', 'launchd job 異常（可能 backoff 鎖死，需 bootout+bootstrap reload）: ' + '、'.join(bad)
    return 'ok', f'{len(jobs)} 個排程 job 正常'


DEFAULT_CHECKS = [
    ('JWT Token', check_token),
    ('磁碟空間', check_disk),
    ('同步狀態', check_last_sync),
    ('API 連線', check_api),
    ('排程 Job', check_launchd_jobs),
    ('上傳暫停', check_pause),
    ('解析積壓', check_parse_backlog),
    ('解析預算', check_budget),
    ('清單新鮮度', check_list_freshness),
    ('來源資料夾', check_folder_deaths),
    ('未支援來源', check_unsupported_sources),
    ('同步錯誤', check_sync_errors),
]


def run_health_check(checks=None, notify=False, alert_state=None, now=None, send=None):
    """跑所有檢查；異常經 AlertState 去重後才通知。

    以前每天把同一句話再發一次（上傳暫停連發 25 天），人麻痺後真正的新告警
    （token 過期連喊 4 天）被淹沒。現在只在「新出現 / 升級 / 每 7 天提醒 / 解除」
    時發一則留言，且只有 error 級才 @ 人（ClickUp 只對被 @ 的人推播）。

    Args:
        checks: [(name, fn)]，預設 DEFAULT_CHECKS
        notify: 是否發送
        alert_state: AlertState 實例（測試可注入 tmp 路徑）
        now: 現在時間（測試可注入）
        send: 發送函式 send(title, body, mention)（測試可注入）
    Returns:
        (issues, events)
    """
    from datetime import datetime as _dt
    from geobingan_sync.alert_state import AlertState, format_events

    checks = checks if checks is not None else DEFAULT_CHECKS
    # 獨立 namespace：不與 record_sync_result 共用狀態，免得互相誤判恢復
    alert_state = alert_state or AlertState(namespace='healthcheck')
    now = now or _dt.now()
    icons = {'ok': '✅', 'warning': '⚠️', 'error': '❌'}
    issues = []
    current = {}

    print(f"🏥 系統健康檢查 — {now.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)
    for name, check_fn in checks:
        try:
            status, message = check_fn()
        except Exception as e:
            status, message = 'error', str(e)
        icon = icons.get(status, '❓')
        print(f"  {icon} {name}: {message}")
        if status != 'ok':
            issues.append(f'{icon} {name}: {message}')
            current[name] = (status, message)

    print("=" * 50)
    if issues:
        print(f"⚠️  {len(issues)} 個問題需要關注")
    else:
        print("✅ 所有檢查通過")

    if not notify:
        # 乾跑/手動檢查唯讀：只算不寫，免得悄悄把問題標成「已看過」
        events = alert_state.plan(current, now=now)
        if events:
            print(f"  （乾跑）將通知事件：{[e.kind + ':' + e.key for e in events]}")
        elif issues:
            print(f"  （{len(issues)} 個問題持續中，已抑制重複通知）")
        return issues, events

    if send is None:
        send = clickup_send
    # plan → send → commit：ClickUp 真的送達才落狀態；失敗保留舊狀態、下輪重試
    events, delivered = alert_state.process(current, send=send, now=now)
    if not events:
        if issues:
            print(f"  （{len(issues)} 個問題持續中，已抑制重複通知）")
    else:
        print(f"  通知事件：{[e.kind + ':' + e.key for e in events]}")
        print("  通知已送達" if delivered else "  通知未送達，下輪重試")
    return issues, events


def clickup_send(title, body, needs_attention):
    """預設發送器。needs_attention＝這批含 error 級事件（**含 error 恢復**）。

    送達判準分兩級（2026-09-16 實測後調整）：
    - 需打擾（error 新增／升級／提醒／恢復）：以 **Email** 是否成功為準——ClickUp
      對 Zhe 本人不會推播（機器人用他的 token 發文，自我 @ 會被吃掉、自己發的
      留言也不通知），只算紀錄。
    - 只有 warning：ClickUp 成功即可（純紀錄，不需要打擾）。
    未設定 Email 時退回看 ClickUp，避免整條通道卡死不發。
    """
    from geobingan_sync.notify import send_notification
    from geobingan_sync.config import ALERT_EMAIL_TO, ALERT_SMTP_PASSWORD
    results = send_notification(title, body, use_clickup=True, mention=needs_attention,
                                use_email=needs_attention)
    got = dict(results or [])
    if needs_attention and ALERT_EMAIL_TO and ALERT_SMTP_PASSWORD:
        return bool(got.get('Email'))
    return bool(got.get('ClickUp'))


def main():
    parser = argparse.ArgumentParser(description='系統健康檢查')
    parser.add_argument('--notify', action='store_true', help='異常時發送通知')
    args = parser.parse_args()
    run_health_check(notify=args.notify)


if __name__ == '__main__':
    main()
