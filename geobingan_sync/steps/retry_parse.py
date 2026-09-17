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
from typing import List

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


def fetch_retryable(ids: List[str], headers) -> List[str]:
    """只挑目前確實可重試（pending/failed）的，避免把額度浪費在會被 400 退回的。"""
    out = []
    for i in ids:
        try:
            d = requests.get(f'{_base()}/api/reports/construction-reports/{i}/',
                             headers=headers, timeout=20).json()
        except Exception:
            continue
        if d.get('parse_status') in RETRYABLE_STATUSES:
            out.append(i)
    return out


def main(ids: List[str], max_items: int = 0, yes: bool = False) -> int:
    headers = {'Authorization': f'Bearer {_get_valid_token()}'}
    print(f'📋 候選 {len(ids)} 份，查詢目前可重試的…')
    targets = fetch_retryable(ids, headers)
    print(f'   可重試（pending/failed）: {len(targets)} 份')
    if max_items and max_items > 0:
        targets = targets[:max_items]
        print(f'   依 --max 取前 {len(targets)} 份')
    if not targets:
        print('✅ 沒有需要重推的報告')
        return 0

    mb = DailyBudget(cost_per_report=COST_PER_REPORT_USD)
    day = mb.load()
    print(f"  💰 今日({day['day']}, UTC) 上傳 {day['uploaded']}＋重推 {day['retried']} "
          f"＝ {day['units']} 份 ≈ US${day['est_usd']:.2f} / 日上限 US${DAILY_BUDGET_USD:.0f}")

    # 與上傳相同的守門：單次門檻對原始請求量 → 鎖內原子預留（記在 retried）
    reserved, msgs, blocked, reserved_day = gate_and_reserve(
        mb, len(targets), DAILY_BUDGET_USD, COST_PER_REPORT_USD, BUDGET_CONFIRM_USD, yes,
        kind='retried')
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
    return 0


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='重推卡住的報告（先預留日額度再送出）')
    ap.add_argument('--ids-file', required=True, help='每行一個 report id 的檔案')
    ap.add_argument('--max', type=int, default=0, help='本次最多重推幾份（0＝由日額度決定）')
    ap.add_argument('--yes', action='store_true',
                    help='估算成本超過 BUDGET_CONFIRM_USD 時仍執行（請先確認後端預算餘裕）')
    a = ap.parse_args()
    with open(a.ids_file, encoding='utf-8') as f:
        ids = [l.strip() for l in f if l.strip()]
    sys.exit(main(ids, max_items=a.max, yes=a.yes))
