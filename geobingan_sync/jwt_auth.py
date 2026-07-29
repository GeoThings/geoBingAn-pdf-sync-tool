"""
JWT Token 管理模組

提供 JWT Token 的解碼、過期檢查、刷新功能。
供 upload_pdfs.py 和 generate_permit_tracking_report.py 共用。
"""
import base64
import json
import time
import threading
import requests
from typing import Optional


# 執行緒安全鎖，保護 token 刷新操作
_token_lock = threading.Lock()


def decode_jwt_payload(token: str) -> dict:
    """解碼 JWT Token 的 payload（不驗證簽名）"""
    try:
        # JWT 格式: header.payload.signature
        parts = token.split('.')
        if len(parts) != 3:
            return {}

        # Base64 解碼 payload（需要處理 padding）
        payload = parts[1]
        # 添加 padding
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += '=' * padding

        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def is_token_expired(token: str, buffer_seconds: int = 300) -> bool:
    """
    檢查 Token 是否已過期或即將過期

    Args:
        token: JWT Token
        buffer_seconds: 提前多少秒視為過期（預設 5 分鐘）

    Returns:
        True 如果已過期或即將過期
    """
    payload = decode_jwt_payload(token)
    if not payload:
        return True

    exp = payload.get('exp')
    if not exp:
        return True

    # 檢查是否過期（加上緩衝時間）
    current_time = time.time()
    return current_time >= (exp - buffer_seconds)


def refresh_access_token(refresh_token: str, refresh_url: str) -> tuple:
    """
    使用 refresh_token 取得新的 access_token（及可能的新 refresh_token）

    Returns:
        tuple of (new_access_token, new_refresh_token) — 任一可能為 None
    """
    try:
        print("🔄 正在刷新 JWT Token...", flush=True)

        response = requests.post(
            refresh_url,
            json={'refresh_token': refresh_token},
            headers={'Content-Type': 'application/json'},
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            new_token = data.get('access') or data.get('access_token')
            new_refresh = data.get('refresh') or data.get('refresh_token')

            if new_token:
                print("✅ JWT Token 刷新成功", flush=True)
                if new_refresh:
                    print("✅ Refresh Token 也已更新", flush=True)
                return new_token, new_refresh
            else:
                print(f"❌ 刷新回應中找不到 access token: {data}", flush=True)
                return None, None
        else:
            print(f"❌ Token 刷新失敗 ({response.status_code}): {response.text[:200]}", flush=True)
            return None, None

    except Exception as e:
        print(f"❌ Token 刷新發生錯誤: {e}", flush=True)
        return None, None


def get_valid_token(current_token: str, refresh_token: str, refresh_url: str) -> tuple:
    """
    取得有效的 access token（執行緒安全）

    Returns:
        tuple of (valid_token, was_refreshed, new_refresh_token)
    """
    with _token_lock:
        if is_token_expired(current_token):
            print("⚠️  JWT Token 已過期或即將過期", flush=True)
            new_token, new_refresh = refresh_access_token(refresh_token, refresh_url)
            if new_token:
                return new_token, True, new_refresh
            else:
                print("⚠️  使用舊 Token 嘗試（可能會失敗）", flush=True)

        return current_token, False, None
