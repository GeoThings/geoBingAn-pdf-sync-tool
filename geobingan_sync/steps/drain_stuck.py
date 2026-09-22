#!/usr/bin/env python3
"""隔日自動放行：把我方近 7 天上傳、卡在 pending／failed 的報告用 retry-parse 放行。

為什麼（2026-09-21）：後端撞額度後入列的解析停在 pending、billing 失敗的停在 failed，
兩者都**不會自己恢復**——午夜額度重置不放行、beat 排程不做恢復，唯一出口是手動
retry-parse。原本靠人記得；這支掛在 launchd 08:20（台北午夜重置後、08:00 健康檢查
之後）自動做，卡住從「永久」變「隔天自癒」。

守門（全部沿用既有機制）：
- 先探解析引擎健康（`parser_health.probe`）：帳戶沒餘額或 worker 停擺就不放行——
  retry-parse 端點本身不查預算，同一天重試只會再被擋，白燒 attempt。
- 只挑**我方上傳**的（檔名在 state/upload_history_all.json）、近 N 天建立、狀態
  pending/failed；**排除確定性失敗**（輸出達長度上限那類，重試只燒錢）。
- 送出走 `steps.retry_parse.main`：同一把鎖先預留日額度再送、保守結算。

用法：
    python -m geobingan_sync.steps.drain_stuck                 # 近 7 天、最多 20 份
    python -m geobingan_sync.steps.drain_stuck --days 3 --max 10
    python -m geobingan_sync.steps.drain_stuck --skip-parser-health   # 人工確認後繞過探測
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Set, Tuple

from geobingan_sync.parser_health import classify_error, load_state, probe, save_state, Verdict

HISTORY_FILE = './state/upload_history_all.json'


def norm_name(name: str) -> str:
    """比對鍵：去掉路徑、去頭尾空白、去掉 .pdf 副檔名（不分大小寫）。

    Drive 上有些檔名沒有副檔名（實例：歷史記「兩廳院(基地)(1150907~1150912)NO.72」，
    後端 file_name 是「…NO.72.pdf」），只比原字串會把我方的報告當成別人的而漏放行。
    """
    n = str(name).split('/')[-1].strip()
    if n.lower().endswith('.pdf'):
        n = n[:-4]
    return n


def load_our_names(path: str = HISTORY_FILE) -> Set[str]:
    """上傳歷史的 unique_id 是「資料夾/檔名」，後端報告只有檔名 → 取正規化檔名集合。"""
    if not os.path.exists(path):
        return set()
    with open(path, encoding='utf-8') as f:
        h = json.load(f)
    items = h.get('uploaded_files', h) if isinstance(h, dict) else h
    return {norm_name(u) for u in items}


def _ts(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def select_stuck(reports: List[dict], our_names: Set[str], now: datetime,
                 days: int = 7) -> Tuple[List[str], List[Tuple[str, str]]]:
    """純函式：回 (要放行的 id 清單, 被排除的 (id, 原因))。reports 為含 metadata 的 detail。"""
    cut = now - timedelta(days=days)
    ids, skipped = [], []
    for r in reports:
        rid = r.get('id')
        if norm_name(r.get('file_name') or '') not in our_names:
            continue
        t = _ts(r.get('created_at'))
        if not t or t < cut:
            continue
        status = r.get('parse_status')
        if status not in ('pending', 'failed'):
            continue
        m = r.get('metadata') or {}
        kind = classify_error(m.get('parse_error') or m.get('skip_reason'))
        if kind == 'deterministic':
            skipped.append((rid, '確定性失敗（輸出超上限），重試只燒錢'))
            continue
        ids.append(rid)
    return ids, skipped


def fetch_candidates(base: str, headers: dict, now: datetime, days: int, get: Callable = None) -> List[dict]:
    """近 days 天建立、pending/failed 的報告（含 detail metadata）。"""
    import requests
    get = get or requests.get
    cut = now - timedelta(days=days)
    out = []
    for status in ('pending', 'failed'):
        url = f'{base}/api/reports/construction-reports/'
        params = {'page_size': 200, 'ordering': '-created_at', 'parse_status': status}
        for _ in range(3):
            resp = get(url, headers=headers, params=params, timeout=40)
            resp.raise_for_status()
            d = resp.json()
            params = None
            page = d.get('results') or []
            recent = [r for r in page if (_ts(r.get('created_at')) or cut) >= cut]
            out.extend(recent)
            if len(recent) < len(page) or not d.get('next'):
                break
            url = d['next']
    detailed = []
    for r in out:
        resp = get(f"{base}/api/reports/construction-reports/{r['id']}/", headers=headers, timeout=30)
        resp.raise_for_status()
        detailed.append(resp.json())
    return detailed


def main(days: int = 7, max_items: int = 20, yes: bool = False, skip_parser_health: bool = False,
         budget_path=None, probe_fn: Callable[[], Verdict] = None, now: datetime = None,
         fetch_fn: Callable = None, retry_fn: Callable = None, our_names: Set[str] = None,
         state_path: str = None) -> int:
    """可注入探測／抓取／重推函式與時鐘供測試；預設走正式路徑。"""
    now = now or datetime.now(timezone.utc)
    v = (probe_fn or probe)()
    print(f"  {'✅' if v.ok else '🛑'} 解析引擎探測：{v.reason}")
    canary = False
    if not v.ok and not skip_parser_health:
        # held 狀態只能靠「看見恢復」解除，但沒人送東西進去就永遠看不見 → 死結。
        # 出口＝每天送 1 份 canary 探路：成本上限 1 份，結果會進下一輪探測的視窗。
        st = load_state(state_path) if state_path else load_state()
        today = (now or datetime.now(timezone.utc)).strftime('%Y-%m-%d')
        if st.get('canary_day') == today:
            print('🛑 不放行：解析引擎異常，今日 canary 已送過。等後端修復；'
                  '人工確認後可加 --skip-parser-health。')
            return 4
        canary = True
        print('  🐤 送 1 份 canary 探路（held 需要「看見恢復」才能解除，不送就永遠解不開）')
    elif not v.ok:
        print('  ⚠️ 已指定 --skip-parser-health，照常放行')

    if fetch_fn is None:
        from geobingan_sync.steps.upload_pdfs import _get_valid_token
        from geobingan_sync.config import GEOBINGAN_BASE_URL
        base = GEOBINGAN_BASE_URL.rstrip('/')
        headers = {'Authorization': f'Bearer {_get_valid_token()}'}
        reports = fetch_candidates(base, headers, now, days)
    else:
        reports = fetch_fn()
    names = our_names if our_names is not None else load_our_names()
    ids, skipped = select_stuck(reports, names, now, days=days)
    print(f'📋 近 {days} 天我方上傳的卡住報告：{len(ids)} 份可放行、{len(skipped)} 份確定性失敗不重試')
    if not ids:
        print('✅ 沒有需要放行的報告' + ('(canary 無候選可送，不記入今日額度)' if canary else ''))
        return 0
    cap = 1 if canary else max_items
    ids = ids[:cap] if cap > 0 else ids
    label = 'canary 探路 1 份' if canary else f'上限 {max_items}'
    print(f'   本次{label}，實際送 {len(ids)} 份（走 retry_parse：先預留日額度再送）')
    if retry_fn is None:
        from geobingan_sync.steps.retry_parse import main as retry_main
        retry_fn = lambda i: retry_main(i, max_items=max_items, yes=yes, budget_path=budget_path)  # noqa: E731
    rc = retry_fn(ids)
    if canary:
        # 只有真的送出去（有候選、retry 回 0）才算用掉今天的 canary（review P2）。
        # 原本在送出前就寫 canary_day：沒候選或 retry 失敗時會白白鎖掉當日探路，
        # 而 held 的唯一出口就是 canary，等於把自己關到隔天。
        if rc == 0:
            st = load_state(state_path) if state_path else load_state()
            st['canary_day'] = today
            st['canary_sent_at'] = (now or datetime.now(timezone.utc)).isoformat()
            (save_state(st, state_path) if state_path else save_state(st))
        else:
            print(f'  ⚠️ canary 未送成功（retry 回 {rc}），不記入今日額度，下次仍可探路')
    return rc


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='隔日自動放行我方卡住的報告（先探健康、再預留、再送）')
    ap.add_argument('--days', type=int, default=7, help='只看近 N 天我方上傳的（預設 7）')
    ap.add_argument('--max', type=int, default=20, help='本次最多放行幾份（預設 20）')
    ap.add_argument('--yes', action='store_true', help='估算超過 BUDGET_CONFIRM_USD 仍執行（只解單次門檻）')
    ap.add_argument('--skip-parser-health', action='store_true', help='人工確認後繞過解析引擎探測')
    a = ap.parse_args()
    sys.exit(main(days=a.days, max_items=a.max, yes=a.yes, skip_parser_health=a.skip_parser_health))
