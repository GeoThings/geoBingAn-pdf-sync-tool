#!/usr/bin/env python3
"""
記錄同步執行結果並發送通知

此腳本從環境變數讀取執行結果，避免 shell 變數注入問題。

環境變數：
- SYNC_STATUS: 執行狀態 (success/failure)
- SYNC_RUN_STARTED: 本輪開始時間（ISO 或 epoch）；用來判定結果檔是本輪的還是上一輪殘留
- SYNC_UPLOAD_SKIPPED: 上傳步驟被跳過的原因（例如 paused）；有值代表「確定沒上傳」
- SYNC_DURATION_SECONDS: 執行秒數
- SYNC_ERROR_MESSAGE: 錯誤訊息（失敗時）

數量不再由 shell grep 日誌反推（2026-09-30：三個計數器都靜默失準），改由各步驟
寫進 state/step_result_*.json。讀不到就是 None＝**未取得**，不是 0。
"""

import os
import sys

# 確保可以 import 同目錄的模組
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from geobingan_sync.sync_status import SyncStatus
from geobingan_sync.notify import send_success, send_notification
from geobingan_sync.alert_state import AlertState
from datetime import datetime

SYNC_ALERT_KEY = '同步執行'


def read_counts(run_started: str = '', upload_skipped: str = '', base: str = None):
    """從各步驟寫出的結果檔取數量。拿不到回 None（未取得），**不回 0**。

    「沒量到」與「量到是 0」必須分得開。回 0 就是把量測失敗偽裝成正常結果，
    那正是舊版 grep 計數的毛病：119 次執行都報 0，沒人發現樣式早就不匹配。

    上傳步驟被跳過（例如 .pause_upload）時是**確定沒上傳**，回 0 才對——
    那是量到的 0，不是沒量到。
    """
    from geobingan_sync import step_results
    not_before = step_results.parse_run_started(run_started)
    sync = step_results.read(step_results.SYNC, not_before=not_before, base=base)
    synced = sync.get('synced') if isinstance(sync, dict) else None

    if upload_skipped:
        return synced, 0, 0
    up = step_results.read(step_results.UPLOAD, not_before=not_before, base=base)
    if not isinstance(up, dict):
        return synced, None, None
    return synced, up.get('uploaded'), up.get('failed')


def main():
    # 從環境變數讀取（安全，不會有注入問題）
    status = os.environ.get('SYNC_STATUS', 'success')
    duration_seconds = int(os.environ.get('SYNC_DURATION_SECONDS', '0'))
    error_message = os.environ.get('SYNC_ERROR_MESSAGE', '')
    synced_count, uploaded_count, failed_count = read_counts(
        run_started=os.environ.get('SYNC_RUN_STARTED', ''),
        upload_skipped=os.environ.get('SYNC_UPLOAD_SKIPPED', ''))

    # 記錄執行結果
    sync_status = SyncStatus()
    result = sync_status.end_run(
        status=status,
        synced_pdfs=synced_count,
        uploaded_pdfs=uploaded_count,
        failed_uploads=failed_count,
        error_message=error_message if status == 'failure' else None,
        duration_seconds=duration_seconds
    )

    # 發送通知
    duration_minutes = duration_seconds / 60.0
    if status == 'success':
        send_success(
            synced=synced_count,
            uploaded=uploaded_count,
            failed=failed_count,
            duration_minutes=duration_minutes
        )

    # 失敗/恢復經 AlertState：連日失敗不洗版、恢復時發一則 ✅（9/1 磁碟滿沒人知的補洞）
    notify_sync_outcome(status, error_message)

    return 0 if status == 'success' else 1


def notify_sync_outcome(status: str, error_message: str, alert_state=None, now=None, send=None):
    """同步失敗→ClickUp 並 @；持續失敗每 7 天提醒；恢復→✅。可注入依賴供測試。

    獨立 namespace='sync'（不與 health_check 共用狀態）；plan→send→commit，
    ClickUp 未送達就不落狀態、下輪重試。
    """
    alert_state = alert_state or AlertState(namespace='sync')
    now = now or datetime.now()
    current = {} if status == 'success' else {SYNC_ALERT_KEY: ('error', error_message or '執行失敗')}
    if send is None:
        send = _clickup_send
    events, _delivered = alert_state.process(current, send=send, now=now)
    return events


def _clickup_send(title, body, needs_attention):
    """同步失敗與其恢復都算 error 級，一律寄 Email（唯一會推播到人）並以其為送達判準。"""
    from geobingan_sync.config import ALERT_EMAIL_TO, ALERT_SMTP_PASSWORD
    results = send_notification(title, body, use_clickup=True, mention=needs_attention,
                                use_email=needs_attention)
    got = dict(results or [])
    if needs_attention and ALERT_EMAIL_TO and ALERT_SMTP_PASSWORD:
        return bool(got.get('Email'))
    return bool(got.get('ClickUp'))


if __name__ == '__main__':
    sys.exit(main())
