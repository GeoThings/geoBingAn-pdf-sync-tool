#!/usr/bin/env python3
"""
記錄同步執行結果並發送通知

此腳本從環境變數讀取執行結果，避免 shell 變數注入問題。

環境變數：
- SYNC_STATUS: 執行狀態 (success/failure)
- SYNC_SYNCED_COUNT: 同步的 PDF 數量
- SYNC_UPLOADED_COUNT: 上傳的 PDF 數量
- SYNC_FAILED_COUNT: 上傳失敗的數量
- SYNC_DURATION_SECONDS: 執行秒數
- SYNC_ERROR_MESSAGE: 錯誤訊息（失敗時）
"""

import os
import sys

# 確保可以 import 同目錄的模組
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from geobingan_sync.sync_status import SyncStatus
from geobingan_sync.notify import send_success, send_notification
from geobingan_sync.alert_state import AlertState, format_events
from datetime import datetime

SYNC_ALERT_KEY = '同步執行'


def main():
    # 從環境變數讀取（安全，不會有注入問題）
    status = os.environ.get('SYNC_STATUS', 'success')
    synced_count = int(os.environ.get('SYNC_SYNCED_COUNT', '0'))
    uploaded_count = int(os.environ.get('SYNC_UPLOADED_COUNT', '0'))
    failed_count = int(os.environ.get('SYNC_FAILED_COUNT', '0'))
    duration_seconds = int(os.environ.get('SYNC_DURATION_SECONDS', '0'))
    error_message = os.environ.get('SYNC_ERROR_MESSAGE', '')

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
    """同步失敗→ClickUp 並 @；持續失敗每 7 天提醒；恢復→✅。可注入依賴供測試。"""
    alert_state = alert_state or AlertState()
    now = now or datetime.now()
    current = {} if status == 'success' else {SYNC_ALERT_KEY: ('error', error_message or '執行失敗')}
    events = alert_state.process(current, now=now)
    if not events:
        return events
    title, body, needs_mention = format_events(events, now)
    if send is None:
        send = lambda t, b, m: send_notification(t, b, use_clickup=True, mention=m)
    try:
        send(title, body, needs_mention)
    except Exception as e:
        print(f"  同步結果通知失敗: {e}")
    return events


if __name__ == '__main__':
    sys.exit(main())
