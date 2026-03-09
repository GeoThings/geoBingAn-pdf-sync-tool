"""
配置檔案 - 從環境變數載入 geoBingAn API 認證資訊

使用方式：
1. 複製 .env.example 為 .env
2. 填入實際的認證資訊
3. 執行腳本會自動載入 .env

⚠️ 注意：.env 檔案包含敏感資訊，請勿提交到 Git
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 載入 .env 檔案
env_path = Path(__file__).parent / '.env'
load_dotenv(env_path)

# JWT Token（會自動刷新）
JWT_TOKEN = os.environ.get('JWT_TOKEN', '')

# Refresh Token（用於自動刷新 access token，有效期 7 天）
REFRESH_TOKEN = os.environ.get('REFRESH_TOKEN', '')

# 用戶資訊
USER_EMAIL = os.environ.get('USER_EMAIL', '')
USER_ID = os.environ.get('USER_ID', '')

# 群組資訊
GROUP_ID = os.environ.get('GROUP_ID', '')
GROUP_NAME = os.environ.get('GROUP_NAME', '')

# API 設定
GEOBINGAN_BASE_URL = os.environ.get('GEOBINGAN_BASE_URL', 'https://riskmap.today')
GEOBINGAN_API_URL = os.environ.get('GEOBINGAN_API_URL', 'https://riskmap.today/api/reports/construction-reports/upload/')
GEOBINGAN_REFRESH_URL = os.environ.get('GEOBINGAN_REFRESH_URL', 'https://riskmap.today/api/auth/auth/refresh_token/')

# Google Drive 設定
GOOGLE_CREDENTIALS = os.environ.get('GOOGLE_CREDENTIALS', './credentials.json')
SHARED_DRIVE_ID = os.environ.get('SHARED_DRIVE_ID', '')

# 通知設定
LINE_NOTIFY_TOKEN = os.environ.get('LINE_NOTIFY_TOKEN', '')
ENABLE_MACOS_NOTIFY = os.environ.get('ENABLE_MACOS_NOTIFY', 'true').lower() == 'true'

# 同步設定
DAYS_AGO = int(os.environ.get('DAYS_AGO', '7'))
MAX_UPLOADS = int(os.environ.get('MAX_UPLOADS', '0'))
DELAY_BETWEEN_UPLOADS = int(os.environ.get('DELAY_BETWEEN_UPLOADS', '2'))


def update_jwt_token(new_token: str, new_refresh_token: str = None):
    """
    更新 .env 檔案中的 JWT Token

    Args:
        new_token: 新的 access token
        new_refresh_token: 新的 refresh token（可選）
    """
    global JWT_TOKEN, REFRESH_TOKEN

    if not env_path.exists():
        print("警告：.env 檔案不存在，無法更新 Token")
        return

    content = env_path.read_text(encoding='utf-8')

    # 更新 JWT_TOKEN
    import re
    content = re.sub(
        r'^JWT_TOKEN=.*$',
        f'JWT_TOKEN={new_token}',
        content,
        flags=re.MULTILINE
    )
    JWT_TOKEN = new_token

    # 更新 REFRESH_TOKEN（如果提供）
    if new_refresh_token:
        content = re.sub(
            r'^REFRESH_TOKEN=.*$',
            f'REFRESH_TOKEN={new_refresh_token}',
            content,
            flags=re.MULTILINE
        )
        REFRESH_TOKEN = new_refresh_token

    env_path.write_text(content, encoding='utf-8')
    print("✅ Token 已更新到 .env 檔案")
