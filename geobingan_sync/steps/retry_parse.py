#!/usr/bin/env python3
"""重推卡住的報告（retry-parse），**先預留日額度再送出**。

為什麼要有這支：手動 retry 與夜間上傳吃同一份後端日額度。若採「送完再記帳」，
在 retry 已送出、尚未記帳的空窗裡，夜間上傳會讀到用量為 0 而照常預留，兩者合計
就超過日上限——2026-09-14 把額度打爆、116 份卡死就是同一類問題（review P1）。
因此這支走與上傳完全相同的守門：單次門檻 → 鎖內原子預留 → 逐份送出 → 保守結算。

用法：
    python -m geobingan_sync.steps.retry_parse --ids-file state/.sep_upload_ids_20260914.txt
    python -m geobingan_sync.steps.retry_parse --pending-from FILE --max 55 --yes
"""
import sys
import time
from typing import List, Tuple

import requests

from geobingan_sync.budget import (DailyBudget, ReservationLedger, gate_and_reserve)
from geobingan_sync.config import (COST_PER_REPORT_USD, DAILY_BUDGET_USD,
                                   BUDGET_CONFIRM_USD, GEOBINGAN_BASE_URL)
from geobingan_sync.steps.upload_pdfs import _get_valid_token

RETRYABLE_STATUSES = ('pending', 'failed')


def _base():
    return GEOBINGAN_BASE_URL.rstrip('/')


def classify_response(status_code: int) -> str:
    """把 retry-parse 的回應分類成預算結算用的三態（與上傳同一套語意）。

    - 202：已受理 → 視為已消耗
    - 4xx：明確拒絕（例如非 failed/pending 而回 400）→ 確定零成本，可退還
    - 5xx／其他：結果不明 → 保守視為已消耗，不退
    """
    if status_code == 202:
        return 'accepted'
    if 400 <= status_code < 500:
        return 'rejected'
    return 'unknown'


def fetch_retryable(ids: List[str], headers) -> Tuple[List[str], List[Tuple[str, str]]]:
    """回傳 (可重試的 id, 查詢失敗的 (id, 原因))。

    **查詢失敗絕不當成「不需重推」**（review P2）。原本所有例外一律 continue，
    於是網路中斷、後端 500、401 過期、非 JSON 回應都被歸進「這份不用重推」；
    整批查詢都掛掉時會印出「✅ 沒有需要重推的報告」並以 exit 0 結束，操作者
    以為積壓清空了，其實是一份都沒查到。這正是本專案一路在修的無聲失敗。

    所以改成 fail-closed：查得到才判斷，查不到就進失敗清單，由呼叫端決定要不要
    繼續——而且絕不允許在有失敗的情況下宣告「沒有需要重推」。
    """
    out: List[str] = []
    failures: List[Tuple[str, str]] = []
    for i in ids:
        try:
            r = requests.get(f'{_base()}/api/reports/construction-reports/{i}/',
                             headers=headers, timeout=20)
            if r.status_code != 200:
                failures.append((i, f'HTTP {r.status_code}'))
                continue
            d = r.json()
        except Exception as e:
            failures.append((i, f'{type(e).__name__}: {str(e)[:60]}'))
            continue
        if not isinstance(d, dict):
            failures.append((i, f'回應格式異常（{type(d).__name__}）'))
            continue
        status = d.get('parse_status')
        if status is None:
            failures.append((i, '回應缺少 parse_status'))
        elif status in RETRYABLE_STATUSES:
            out.append(i)
    return out, failures


def main(ids: List[str], max_items: int = 0, yes: bool = False,
         override_daily_budget: str = '', budget_path=None) -> int:
    """budget_path 僅供測試注入：預設走正式帳本 state/upload_budget.json。

    測試若用到正式帳本就會污染當日額度（本檔曾把 retried 由 58 改成 59），
    與 PR #84 的 /tmp/permit_list.pdf 是同一類問題，因此路徑必須可注入。
    """
    headers = {'Authorization': f'Bearer {_get_valid_token()}'}
    print(f'📋 候選 {len(ids)} 份，查詢目前可重試的…')
    targets, failures = fetch_retryable(ids, headers)
    print(f'   可重試（pending/failed）: {len(targets)} 份')
    if failures:
        print(f'   ⚠️ 查詢失敗 {len(failures)} 份（**不代表不需重推**，狀態未知）：')
        for i, why in failures[:5]:
            print(f'      {i[:12]}… {why}')
        if len(failures) > 5:
            print(f'      …另外 {len(failures) - 5} 份')
    if max_items and max_items > 0:
        targets = targets[:max_items]
        print(f'   依 --max 取前 {len(targets)} 份')
    if not targets:
        if failures:
            # 有查不到的就不能宣告「沒有需要重推」——那是把「不知道」講成「沒有」
            print(f'\n🛑 查無可重試的報告，但有 {len(failures)} 份查詢失敗，狀態未知。'
                  f'\n   不宣告「沒有需要重推」；請先排除查詢失敗（網路／token／後端）再重跑。')
            return 4
        print('✅ 沒有需要重推的報告')
        return 0

    mb = DailyBudget(path=budget_path, cost_per_report=COST_PER_REPORT_USD)
    day = mb.load()
    print(f"  💰 今日({day['day']}, UTC) 上傳 {day['uploaded']}＋重推 {day['retried']} "
          f"＝ {day['units']} 份 ≈ US${day['est_usd']:.2f} / 日上限 US${DAILY_BUDGET_USD:.0f}")

    # 與上傳相同的守門：單次門檻對原始請求量 → 鎖內原子預留（記在 retried）
    if override_daily_budget:
        print(f'  🚨 已指定 --override-daily-budget：將突破後端日上限（理由：{override_daily_budget}）')
    reserved, msgs, blocked, reserved_day = gate_and_reserve(
        mb, len(targets), DAILY_BUDGET_USD, COST_PER_REPORT_USD, BUDGET_CONFIRM_USD, yes,
        kind='retried', override=bool(override_daily_budget),
        override_reason=override_daily_budget)
    for m in msgs:
        print(f'  💰 {m}')
    if blocked:
        print(f'\n🛑 已擋下：{blocked}')
        return 3
    if reserved < len(targets):
        targets = targets[:reserved]
        print(f'  重推份數（裁切後）: {len(targets)}')

    ledger = ReservationLedger(mb, reserved, day=reserved_day, kind='retried')
    accepted = rejected = unknown = 0
    try:
        for n, rid in enumerate(targets, 1):
            if not ledger.begin_item():
                print(f'\n⏹️  已跨日（預留屬 {ledger.day}），停止本批次；'
                      f'剩餘 {len(targets) - n + 1} 份待下次於新的一天重新預留')
                break
            try:
                r = requests.post(f'{_base()}/api/reports/construction-reports/{rid}/retry-parse/',
                                  headers=headers, timeout=30)
                kind = classify_response(r.status_code)
            except Exception as e:
                kind = 'unknown'
                print(f'  ⚠️ {rid[:12]}… {type(e).__name__}: {str(e)[:60]}')
            if kind == 'accepted':
                accepted += 1
            elif kind == 'rejected':
                rejected += 1
                ledger.settle({'success': False, 'error': 'rejected'})   # 確定零成本 → 退還
                print(f'  ⚠️ {rid[:12]}… 後端拒絕（HTTP {r.status_code}），已退還額度')
            else:
                unknown += 1                                              # 結果不明 → 保守保留
            if n % 20 == 0:
                print(f'    …已送出 {n}/{len(targets)}', flush=True)
                headers = {'Authorization': f'Bearer {_get_valid_token()}'}
            time.sleep(0.3)
    finally:
        d = ledger.close()
        print(f"\n💰 今日累計 {d['units']} 份（上傳 {d['uploaded']}＋重推 {d['retried']}）"
              f" ≈ US${d['est_usd']:.2f} / 日上限 US${DAILY_BUDGET_USD:.0f}")

    print(f'\n📊 送出結果：受理 {accepted}、拒絕 {rejected}、結果不明 {unknown}')
    print('   受理的報告需要時間解析，可用 health_check 的「解析積壓」追蹤')
    if failures:
        print(f'\n⚠️ 另有 {len(failures)} 份當初查詢失敗、未納入本批次，狀態仍未知；'
              f'排除原因後請重跑一次確認。')
        return 4
    return 0


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='重推卡住的報告（先預留日額度再送出）')
    ap.add_argument('--ids-file', required=True, help='每行一個 report id 的檔案')
    ap.add_argument('--max', type=int, default=0, help='本次最多重推幾份（0＝由日額度決定）')
    ap.add_argument('--yes', action='store_true',
                    help='估算成本超過 BUDGET_CONFIRM_USD 時仍執行（請先確認後端預算餘裕）；'
                         '**只解單次門檻，不會放寬後端日上限**')
    ap.add_argument('--override-daily-budget', metavar='REASON', default='',
                    help='突破後端每日額度硬上限，需填理由（會記進日誌）。'
                         '僅限人工、且已與後端確認可超支時使用；排程不得帶此旗標')
    a = ap.parse_args()
    with open(a.ids_file, encoding='utf-8') as f:
        ids = [l.strip() for l in f if l.strip()]
    sys.exit(main(ids, max_items=a.max, yes=a.yes,
                  override_daily_budget=a.override_daily_budget))
