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


def check_last_sync():
    """檢查最近一次同步狀態"""
    status_file = './state/sync_status.json'
    if not os.path.exists(status_file):
        return 'warning', '找不到同步狀態檔案'

    try:
        with open(status_file, 'r') as f:
            data = json.load(f)
        last_run = data.get('last_run', '')
        last_status = data.get('last_status', '')
        if not last_run:
            return 'warning', '尚未執行過同步'

        days_ago = (datetime.now() - datetime.fromisoformat(last_run)).days
        if days_ago > 10:
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
    """本月解析估算成本 vs MONTHLY_BUDGET_USD：≥70% warning、≥90% error。path 可注入供測試。"""
    from geobingan_sync.budget import MonthlyBudget, budget_level
    from geobingan_sync.config import COST_PER_REPORT_USD, MONTHLY_BUDGET_USD
    try:
        m = MonthlyBudget(path=path, cost_per_report=COST_PER_REPORT_USD).load()
        level, ratio = budget_level(float(m.get('est_usd', 0)), MONTHLY_BUDGET_USD)
        msg = f"本月({m['month']})已傳 {m['uploaded']} 份 ≈ US${float(m.get('est_usd', 0)):.2f} / 上限 US${MONTHLY_BUDGET_USD:.0f}（{ratio:.0%}）"
        if level != 'ok':
            msg += '；請與 slayer 確認預算餘裕再上傳'
        return level, msg
    except Exception as e:
        return 'warning', f'預算檢查失敗: {e}'


def check_launchd_jobs():
    """檢查三個 launchd 排程 job 的最後執行狀態。

    動機：launchd 對 EX_CONFIG 等錯誤會 silent backoff 鎖死整個 schedule、
    沒有任何 alerting。fridayreport 5/1 鎖 3 週、weeklysync 4 月某次鎖 4 週
    都是下游發現「咦週報沒進來」才察覺。把巡檢納入每日健檢、stuck 立刻浮現。

    註：用 `gui/{uid}` domain 查 LaunchAgent，需在 GUI (Aqua) session 執行。
    純 SSH session（無 GUI）可能查不到、會回報「無法解析」— 屬預期保守誤報。
    """
    import subprocess
    import re
    jobs = ['healthcheck', 'weeklysync', 'fridayreport']
    uid = os.getuid()
    bad = []
    for job in jobs:
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
        elif m.group(1) not in ('0', '(never'):
            bad.append(f'{job}（last exit={m.group(1)}）')
    if bad:
        return 'warning', 'launchd job 異常（可能 backoff 鎖死，需 bootout+bootstrap reload）: ' + '、'.join(bad)
    return 'ok', '三個排程 job 正常'


DEFAULT_CHECKS = [
    ('JWT Token', check_token),
    ('磁碟空間', check_disk),
    ('同步狀態', check_last_sync),
    ('API 連線', check_api),
    ('排程 Job', check_launchd_jobs),
    ('上傳暫停', check_pause),
    ('解析積壓', check_parse_backlog),
    ('解析預算', check_budget),
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


def clickup_send(title, body, mention):
    """預設發送器：回傳 ClickUp 通道是否真的成功（其他通道不算數）。"""
    from geobingan_sync.notify import send_notification
    results = send_notification(title, body, use_clickup=True, mention=mention)
    return any(ch == 'ClickUp' and ok for ch, ok in (results or []))


def main():
    parser = argparse.ArgumentParser(description='系統健康檢查')
    parser.add_argument('--notify', action='store_true', help='異常時發送通知')
    args = parser.parse_args()
    run_health_check(notify=args.notify)


if __name__ == '__main__':
    main()
