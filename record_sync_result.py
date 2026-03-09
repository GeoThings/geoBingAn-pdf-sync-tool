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

from sync_status import SyncStatus
from notify import send_success, send_failure


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
    else:
        send_failure('執行失敗', error_message)

    return 0 if status == 'success' else 1


if __name__ == '__main__':
    sys.exit(main())
