"""
通知模組 - 發送執行狀態通知

支援：
- LINE Notify
- macOS 系統通知

使用方式：
    from notify import send_notification, send_success, send_failure

    # 發送一般通知
    send_notification("同步完成", "已處理 100 個檔案")

    # 發送成功摘要
    send_success(synced=100, uploaded=50, failed=2)

    # 發送失敗通知
    send_failure("API 連線失敗", "無法連接到 riskmap.today")
"""

import os
import subprocess
from typing import Optional

# 載入設定
try:
    from config import LINE_NOTIFY_TOKEN, ENABLE_MACOS_NOTIFY
except ImportError:
    LINE_NOTIFY_TOKEN = os.environ.get('LINE_NOTIFY_TOKEN', '')
    ENABLE_MACOS_NOTIFY = os.environ.get('ENABLE_MACOS_NOTIFY', 'true').lower() == 'true'


def send_line_notify(message: str) -> bool:
    """
    發送 LINE Notify 通知

    Args:
        message: 要發送的訊息

    Returns:
        bool: 發送是否成功
    """
    token = LINE_NOTIFY_TOKEN
    if not token:
        return False

    try:
        import requests
        response = requests.post(
            'https://notify-api.line.me/api/notify',
            headers={'Authorization': f'Bearer {token}'},
            data={'message': message},
            timeout=10
        )
        return response.status_code == 200
    except Exception as e:
        print(f"LINE Notify 發送失敗: {e}")
        return False


def send_macos_notification(title: str, message: str, sound: bool = True) -> bool:
    """
    發送 macOS 系統通知

    Args:
        title: 通知標題
        message: 通知內容
        sound: 是否播放提示音

    Returns:
        bool: 發送是否成功
    """
    if not ENABLE_MACOS_NOTIFY:
        return False

    try:
        safe_title = title.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ').replace('\r', '')
        safe_msg = message.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ').replace('\r', '')
        sound_script = 'with sound name "default"' if sound else ''
        script = f'display notification "{safe_msg}" with title "{safe_title}" {sound_script}'
        subprocess.run(['osascript', '-e', script], capture_output=True, check=True)
        return True
    except Exception as e:
        print(f"macOS 通知發送失敗: {e}")
        return False


def send_notification(title: str, message: str, use_line: bool = True, use_macos: bool = True):
    """
    發送通知（同時使用所有可用的通知方式）

    Args:
        title: 通知標題
        message: 通知內容
        use_line: 是否使用 LINE Notify
        use_macos: 是否使用 macOS 通知
    """
    results = []

    if use_macos:
        macos_result = send_macos_notification(title, message)
        results.append(('macOS', macos_result))

    if use_line and LINE_NOTIFY_TOKEN:
        line_message = f"\n{title}\n{message}"
        line_result = send_line_notify(line_message)
        results.append(('LINE', line_result))

    return results


def send_success(synced: int = 0, uploaded: int = 0, failed: int = 0,
                 duration_minutes: Optional[float] = None):
    """
    發送成功摘要通知

    Args:
        synced: 同步的檔案數
        uploaded: 上傳的檔案數
        failed: 失敗的檔案數
        duration_minutes: 執行時間（分鐘）
    """
    title = "✅ geoBingAn 同步完成"

    parts = []
    if synced > 0:
        parts.append(f"同步: {synced}")
    if uploaded > 0:
        parts.append(f"上傳: {uploaded}")
    if failed > 0:
        parts.append(f"失敗: {failed}")

    message = " | ".join(parts) if parts else "執行完成"

    if duration_minutes:
        message += f"\n耗時: {duration_minutes:.1f} 分鐘"

    send_notification(title, message)


def send_failure(error_type: str, error_message: str):
    """
    發送失敗通知

    Args:
        error_type: 錯誤類型
        error_message: 錯誤訊息
    """
    title = "❌ geoBingAn 同步失敗"
    message = f"{error_type}\n{error_message}"

    send_notification(title, message)


def send_warning(warning_type: str, warning_message: str):
    """
    發送警告通知

    Args:
        warning_type: 警告類型
        warning_message: 警告訊息
    """
    title = "⚠️ geoBingAn 同步警告"
    message = f"{warning_type}\n{warning_message}"

    send_notification(title, message)


if __name__ == '__main__':
    # 測試通知
    print("測試通知模組...")

    print("\n1. 測試 macOS 通知...")
    result = send_macos_notification("測試通知", "這是一個測試訊息")
    print(f"   結果: {'成功' if result else '失敗或已停用'}")

    print("\n2. 測試 LINE Notify...")
    if LINE_NOTIFY_TOKEN:
        result = send_line_notify("\n測試通知\n這是一個測試訊息")
        print(f"   結果: {'成功' if result else '失敗'}")
    else:
        print("   跳過（未設定 LINE_NOTIFY_TOKEN）")

    print("\n3. 測試成功摘要...")
    send_success(synced=100, uploaded=50, failed=2, duration_minutes=45.5)

    print("\n測試完成！")
