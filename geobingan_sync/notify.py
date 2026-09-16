"""
通知模組 - 發送執行狀態通知

支援：
- LINE Notify
- macOS 系統通知

使用方式：
    from geobingan_sync.notify import send_notification, send_success, send_failure

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
    from geobingan_sync.config import (LINE_NOTIFY_TOKEN, ENABLE_MACOS_NOTIFY, CLICKUP_TOKEN,
                                       HEALTHCHECK_CLICKUP_TASK_ID, ALERT_MENTION_USER_ID,
                                       ALERT_EMAIL_TO, ALERT_EMAIL_FROM, ALERT_SMTP_HOST,
                                       ALERT_SMTP_PORT, ALERT_SMTP_PASSWORD)
except ImportError:
    LINE_NOTIFY_TOKEN = os.environ.get('LINE_NOTIFY_TOKEN', '')
    ENABLE_MACOS_NOTIFY = os.environ.get('ENABLE_MACOS_NOTIFY', 'true').lower() == 'true'
    CLICKUP_TOKEN = os.environ.get('CLICKUP_TOKEN', '')
    HEALTHCHECK_CLICKUP_TASK_ID = os.environ.get('HEALTHCHECK_CLICKUP_TASK_ID', '')
    ALERT_MENTION_USER_ID = os.environ.get('ALERT_MENTION_USER_ID', '')
    ALERT_EMAIL_TO = os.environ.get('ALERT_EMAIL_TO', '')
    ALERT_EMAIL_FROM = os.environ.get('ALERT_EMAIL_FROM', '')
    ALERT_SMTP_HOST = os.environ.get('ALERT_SMTP_HOST', 'smtp.gmail.com')
    ALERT_SMTP_PORT = int(os.environ.get('ALERT_SMTP_PORT', '465'))
    ALERT_SMTP_PASSWORD = os.environ.get('ALERT_SMTP_PASSWORD', '')


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
        # launchd context 呼叫 osascript 偶發 exit 1（macOS 環境限制，非業務錯誤）
        # 主要可靠通道為 ClickUp，此處僅記錄
        print(f"  (macOS 通知未送達: {e})")
        return False


def build_clickup_comment_payload(message: str, mention_user_id: Optional[str] = None) -> dict:
    """組 ClickUp comment payload。

    有 mention_user_id 時用結構化 `comment` array + `type:tag` block（純文字 @name 不會
    觸發推播）；否則用 comment_text。獨立成函式方便測試。
    """
    if mention_user_id:
        return {
            'comment': [
                {'type': 'tag', 'user': {'id': int(mention_user_id)}},
                {'text': ' ' + message},
            ],
            'notify_all': False,
        }
    return {'comment_text': message}


def send_clickup_comment(task_id: str, message: str, mention_user_id: Optional[str] = None) -> bool:
    """
    發送 ClickUp task comment

    Args:
        task_id: ClickUp task ID
        message: comment 內容
        mention_user_id: 要 @ 的 ClickUp user id（ClickUp 只對被 @ 的人推播）

    Returns:
        bool: 發送是否成功
    """
    if not CLICKUP_TOKEN or not task_id:
        return False

    try:
        import requests
        response = requests.post(
            f'https://api.clickup.com/api/v2/task/{task_id}/comment',
            headers={'Authorization': CLICKUP_TOKEN, 'Content-Type': 'application/json'},
            json=build_clickup_comment_payload(message, mention_user_id),
            timeout=10
        )
        if response.status_code != 200:
            print(f"ClickUp comment 非 200: {response.status_code} {response.text[:200]}")
        return response.status_code == 200
    except Exception as e:
        print(f"ClickUp comment 發送失敗: {e}")
        return False


def build_email(subject: str, body: str, sender: str, to: str) -> str:
    """組 RFC 訊息字串（中文主旨需 MIME 編碼，否則會變亂碼）。獨立成函式便於測試。"""
    from email.message import EmailMessage
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = to
    msg.set_content(body)
    return msg.as_string()


def send_email_alert(subject: str, body: str) -> bool:
    """用 Gmail SMTP 寄告警信。

    為何需要它：健康檢查由 launchd 於 08:00 自行執行，當下沒有互動 session，
    腳本必須自己會寄信。ClickUp 通道對 Zhe 無效——機器人用他本人的 token 發文，
    ClickUp 不通知自己發的留言、@ 自己也會被吃掉（2026-09-16 實測確認）。

    設定不齊時回 False（不拋例外），由呼叫端決定是否算送達。
    """
    if not (ALERT_EMAIL_TO and ALERT_SMTP_PASSWORD):
        return False
    sender = ALERT_EMAIL_FROM or ALERT_EMAIL_TO
    try:
        import smtplib
        import ssl
        raw = build_email(subject, body, sender, ALERT_EMAIL_TO)
        with smtplib.SMTP_SSL(ALERT_SMTP_HOST, ALERT_SMTP_PORT,
                              context=ssl.create_default_context(), timeout=20) as smtp:
            smtp.login(sender, ALERT_SMTP_PASSWORD)
            smtp.sendmail(sender, [ALERT_EMAIL_TO], raw)
        return True
    except Exception as e:
        print(f"Email 告警發送失敗: {e}")
        return False


def send_notification(title: str, message: str, use_line: bool = True, use_macos: bool = True,
                      use_clickup: bool = False, clickup_task_id: Optional[str] = None,
                      mention: bool = False, use_email: bool = False):
    """
    發送通知（同時使用所有可用的通知方式）

    Args:
        title: 通知標題
        message: 通知內容
        use_line: 是否使用 LINE Notify
        use_macos: 是否使用 macOS 通知
        use_clickup: 是否使用 ClickUp comment（需 CLICKUP_TOKEN 與 task_id）
        clickup_task_id: ClickUp task ID（未提供則用 HEALTHCHECK_CLICKUP_TASK_ID）
        mention: ClickUp 留言是否 @ ALERT_MENTION_USER_ID
            ⚠️ 對 Zhe 本人無效：機器人用他的 token 發文，ClickUp 會吃掉自我 @、
            也不通知自己發的留言（2026-09-16 實測）。要送到人請用 use_email。
        use_email: 是否寄 Email 告警（error 級用；目前唯一實測會推播到手機的通道）
    """
    results = []

    if use_macos:
        macos_result = send_macos_notification(title, message)
        results.append(('macOS', macos_result))

    if use_line and LINE_NOTIFY_TOKEN:
        line_message = f"\n{title}\n{message}"
        line_result = send_line_notify(line_message)
        results.append(('LINE', line_result))

    if use_email:
        results.append(('Email', send_email_alert(title, f'{title}\n\n{message}')))

    if use_clickup:
        task_id = clickup_task_id or HEALTHCHECK_CLICKUP_TASK_ID
        if task_id and CLICKUP_TOKEN:
            clickup_result = send_clickup_comment(
                task_id, f"{title}\n\n{message}",
                mention_user_id=ALERT_MENTION_USER_ID if mention else None,
            )
            results.append(('ClickUp', clickup_result))

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

    # 失敗必須到達人：走 ClickUp 並 @（LINE Notify 已停服、macOS 通知在 launchd 下常送不到）
    # 失敗必須到達人：Email 是實測有效的推播通道；ClickUp 同時保留當紀錄
    send_notification(title, message, use_clickup=True, mention=True, use_email=True)


def send_warning(warning_type: str, warning_message: str):
    """
    發送警告通知

    Args:
        warning_type: 警告類型
        warning_message: 警告訊息
    """
    title = "⚠️ geoBingAn 同步警告"
    message = f"{warning_type}\n{warning_message}"

    send_notification(title, message, use_clickup=True)


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
