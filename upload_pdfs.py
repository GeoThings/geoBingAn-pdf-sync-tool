#!/usr/bin/env python3
"""
上傳最新 5 筆 PDF 到 geoBingAn 分析工具（效能優化版本）

功能：
1. 掃描 Google Drive Shared Drive 中的建案 PDF
2. 只上傳最新的 5 個 PDF 檔案
3. 記錄已上傳的 PDF，避免重複處理
4. 使用 JWT 認證（jerryjo0802@gmail.com）
5. 【新增】並行上傳支援（可選）
6. 【新增】JWT Token 自動刷新
"""
import json
import os
import sys
import io
import time
import base64
import requests
from pathlib import Path
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# 匯入配置檔案
try:
    from config import (
        JWT_TOKEN,
        USER_EMAIL,
        GROUP_ID,
        GEOBINGAN_API_URL,
        REFRESH_TOKEN,
        GEOBINGAN_REFRESH_URL
    )
    print(f"✅ 已載入認證配置（用戶: {USER_EMAIL}）", flush=True)
except ImportError as e:
    print("❌ 找不到 config.py 或缺少必要設定")
    print(f"   錯誤: {e}")
    print("   請參考 config.py.example 建立 config.py")
    sys.exit(1)

# 全域變數：當前使用的 Token
current_access_token = JWT_TOKEN
token_lock = threading.Lock()

# ================== 設定區域 ==================
# Google Drive 認證
SERVICE_ACCOUNT_FILE = os.environ.get(
    'GOOGLE_CREDENTIALS',
    os.path.join(os.path.dirname(__file__), 'credentials.json')
)
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
SHARED_DRIVE_ID = '0AIvp1h-6BZ1oUk9PVA'

# geoBingAn API 設定（從 config.py 匯入）
# GEOBINGAN_API_URL - 已從 config.py 匯入
# 使用 construction-reports/upload/ 端點，與網頁上傳相同

# 狀態追蹤檔案
STATE_FILE = './state/uploaded_to_geobingan_7days.json'
HISTORY_FILE = './state/upload_history_all.json'  # 永久歷史記錄

# 日期過濾設定
DAYS_AGO = 7  # 上傳最近 7 天更新的 PDF

# 批次上傳設定
MAX_UPLOADS = 100  # 每次上傳最新 100 筆 PDF

# 速率控制：每次上傳之間的延遲（秒）
DELAY_BETWEEN_UPLOADS = 2  # 加速：減少到 2 秒

# 並行上傳設定
ENABLE_PARALLEL_UPLOAD = False  # 設為 True 啟用並行上傳（實驗性功能）
MAX_WORKERS = 3  # 並行上傳的最大執行緒數

# 自動確認（測試模式）
AUTO_CONFIRM = True  # 啟用自動確認進行批次上傳

# 排除清單：不上傳的檔案（範例檔、測試檔等）
EXCLUDE_FILES = [
    '雲端資料庫設置之範例.pdf',  # 範例檔案，內含假資料
    '雲端資料庫設置之範例',       # 無副檔名版本
]
# ============================================

# 全域鎖，用於並行上傳時保護狀態檔案
state_lock = threading.Lock()


# ================== JWT Token 管理 ==================

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


def refresh_access_token() -> Optional[str]:
    """
    使用 refresh_token 取得新的 access_token

    Returns:
        新的 access_token，失敗時返回 None
    """
    global current_access_token

    try:
        print("🔄 正在刷新 JWT Token...", flush=True)

        response = requests.post(
            GEOBINGAN_REFRESH_URL,
            json={'refresh_token': REFRESH_TOKEN},
            headers={'Content-Type': 'application/json'},
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            new_token = data.get('access') or data.get('access_token')

            if new_token:
                with token_lock:
                    current_access_token = new_token

                # 更新 config.py 中的 token
                update_config_token(new_token)

                print("✅ JWT Token 刷新成功", flush=True)
                return new_token
            else:
                print(f"❌ 刷新回應中找不到 access token: {data}", flush=True)
                return None
        else:
            print(f"❌ Token 刷新失敗 ({response.status_code}): {response.text[:200]}", flush=True)
            return None

    except Exception as e:
        print(f"❌ Token 刷新發生錯誤: {e}", flush=True)
        return None


def update_config_token(new_token: str):
    """
    更新 config.py 中的 JWT_TOKEN
    """
    config_path = Path(__file__).parent / 'config.py'

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 使用正則表達式替換 token
        import re
        pattern = r"JWT_TOKEN = '[^']+'"
        replacement = f"JWT_TOKEN = '{new_token}'"
        new_content = re.sub(pattern, replacement, content)

        with open(config_path, 'w', encoding='utf-8') as f:
            f.write(new_content)

        print(f"📝 已更新 config.py 中的 Token", flush=True)
    except Exception as e:
        print(f"⚠️  無法更新 config.py: {e}", flush=True)


def get_valid_token() -> str:
    """
    取得有效的 access token

    如果當前 token 即將過期，會自動刷新

    Returns:
        有效的 access token
    """
    global current_access_token

    with token_lock:
        if is_token_expired(current_access_token):
            print("⚠️  JWT Token 已過期或即將過期", flush=True)
            new_token = refresh_access_token()
            if new_token:
                return new_token
            else:
                print("⚠️  使用舊 Token 嘗試（可能會失敗）", flush=True)

        return current_access_token


# ================== 狀態管理 ==================

def load_history() -> dict:
    """載入永久上傳歷史記錄"""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        'uploaded_files': [],
        'total_count': 0,
        'first_upload': None,
        'last_upload': None
    }


def save_history(history: dict):
    """儲存永久上傳歷史記錄"""
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, indent=2, ensure_ascii=False, fp=f)


def add_to_history(filepath: str):
    """新增檔案到永久歷史記錄"""
    history = load_history()

    if filepath not in history['uploaded_files']:
        history['uploaded_files'].append(filepath)
        history['total_count'] = len(history['uploaded_files'])
        history['last_upload'] = datetime.now().isoformat()

        if not history['first_upload']:
            history['first_upload'] = history['last_upload']

        save_history(history)


def load_state() -> dict:
    """載入已上傳的 PDF 記錄（包含快取）"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
            # 確保有快取結構
            if 'cache' not in state:
                state['cache'] = {
                    'folders': [],
                    'pdfs': [],
                    'last_scan': None
                }
            return state
    return {
        'uploaded_files': [],
        'errors': [],
        'cache': {
            'folders': [],
            'pdfs': [],
            'last_scan': None
        }
    }


def save_state(state: dict):
    """儲存已上傳的 PDF 記錄

    注意：此函數不包含鎖，呼叫者需要自行管理 state_lock
    """
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, indent=2, ensure_ascii=False, fp=f)


def get_drive_service():
    """初始化 Google Drive API（Service Account）"""
    print("🔑 初始化 Google Drive API (Service Account)", flush=True)

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print(f"❌ 找不到 Service Account 金鑰: {SERVICE_ACCOUNT_FILE}")
        sys.exit(1)

    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)

    service = build('drive', 'v3', credentials=credentials)
    print(f"✅ 已初始化 ({credentials.service_account_email})", flush=True)
    return service


def list_project_folders(service, use_cache: bool = True, state: dict = None, days_ago: int = 7) -> List[Dict]:
    """
    列出建案資料夾（支援快取和智慧掃描）

    Args:
        service: Google Drive API service
        use_cache: 是否使用快取
        state: 狀態檔案（包含快取）
        days_ago: 只掃描最近 N 天修改的資料夾（智慧掃描）
    """
    # 檢查是否可以使用快取
    if use_cache and state and state.get('cache', {}).get('last_scan'):
        last_scan = state['cache']['last_scan']
        last_scan_time = datetime.fromisoformat(last_scan.replace('Z', '+00:00'))
        now = datetime.now(last_scan_time.tzinfo)

        # 如果上次掃描在 24 小時內，使用快取
        if (now - last_scan_time).total_seconds() < 86400:  # 24 hours
            cached_folders = state['cache'].get('folders', [])
            if cached_folders:
                print(f"✅ 使用快取的資料夾列表（{len(cached_folders)} 個，上次掃描: {last_scan}）")
                return cached_folders

    try:
        # 智慧掃描：只掃描最近 N 天修改的資料夾
        cutoff_date = datetime.now() - timedelta(days=days_ago)
        cutoff_date_str = cutoff_date.isoformat() + 'Z'

        print(f"🔍 智慧掃描: 只列出最近 {days_ago} 天修改的資料夾...")

        query = (
            f"modifiedTime >= '{cutoff_date_str}' and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false"
        )

        results = service.files().list(
            q=query,
            corpora='drive',
            driveId=SHARED_DRIVE_ID,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageSize=1000,
            fields='files(id, name, modifiedTime)'
        ).execute()

        folders = results.get('files', [])

        # 更新快取
        if state is not None:
            state['cache']['folders'] = folders
            state['cache']['last_scan'] = datetime.now().isoformat() + 'Z'

        print(f"✅ 找到 {len(folders)} 個最近 {days_ago} 天修改的資料夾")
        return folders

    except HttpError as e:
        print(f"❌ 列出資料夾失敗: {e}")
        return []


def list_all_pdfs_with_folder_info(service, folders: List[Dict], use_cache: bool = True, state: dict = None) -> List[Dict]:
    """
    列出所有資料夾中的 PDF，並附加資料夾資訊（支援快取）

    Returns:
        List of dict with keys: id, name, size, modifiedTime, folder_id, folder_name
    """
    # 檢查是否可以使用快取
    if use_cache and state and state.get('cache', {}).get('last_scan'):
        last_scan = state['cache']['last_scan']
        last_scan_time = datetime.fromisoformat(last_scan.replace('Z', '+00:00'))
        now = datetime.now(last_scan_time.tzinfo)

        # 如果上次掃描在 24 小時內，使用快取
        if (now - last_scan_time).total_seconds() < 86400:  # 24 hours
            cached_pdfs = state['cache'].get('pdfs', [])
            if cached_pdfs:
                print(f"✅ 使用快取的 PDF 列表（{len(cached_pdfs)} 個）")
                return cached_pdfs

    print(f"🔍 掃描 {len(folders)} 個資料夾中的 PDF...")
    all_pdfs = []

    for idx, folder in enumerate(folders, 1):
        folder_id = folder['id']
        folder_name = folder['name']

        if idx % 10 == 0:
            print(f"  進度: {idx}/{len(folders)} 個資料夾...")

        try:
            query = (
                f"'{folder_id}' in parents and "
                f"mimeType = 'application/pdf' and "
                f"trashed = false"
            )

            results = service.files().list(
                q=query,
                pageSize=1000,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields='files(id, name, size, modifiedTime)'
            ).execute()

            pdfs = results.get('files', [])

            # 為每個 PDF 添加資料夾資訊
            for pdf in pdfs:
                pdf['folder_id'] = folder_id
                pdf['folder_name'] = folder_name
                all_pdfs.append(pdf)

        except HttpError as e:
            print(f"  ❌ 列出 {folder_name} 的 PDF 失敗: {e}")
            continue

    # 更新快取
    if state is not None:
        state['cache']['pdfs'] = all_pdfs

    return all_pdfs


def download_pdf(service, file_id: str, file_name: str, max_retries: int = 3) -> Optional[bytes]:
    """從 Google Drive 下載 PDF（支援網路錯誤重試）"""
    for attempt in range(max_retries):
        try:
            request = service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)

            done = False
            while not done:
                status, done = downloader.next_chunk()

            fh.seek(0)
            content = fh.read()
            print(f"  📥 已下載: {file_name} ({len(content)} bytes)", flush=True)
            return content

        except (ConnectionError, TimeoutError, OSError) as e:
            # 網路錯誤：Connection reset, Timeout 等
            if attempt < max_retries - 1:
                wait_time = 5 * (attempt + 1)  # 漸進等待：5s, 10s, 15s
                print(f"  ⚠️  網路錯誤 ({type(e).__name__}): {e}")
                print(f"  🔄 第 {attempt + 1}/{max_retries} 次重試，等待 {wait_time} 秒...")
                time.sleep(wait_time)
            else:
                print(f"  ❌ 下載失敗（已重試 {max_retries} 次）: {file_name} - {e}")
                return None

        except HttpError as e:
            print(f"  ❌ 下載失敗: {file_name} - {e}")
            return None

        except Exception as e:
            print(f"  ❌ 未預期錯誤: {file_name} - {type(e).__name__}: {e}")
            return None

    return None


def upload_to_geobingan(pdf_content: bytes, file_name: str, project_code: str) -> Optional[dict]:
    """
    上傳 PDF 到 geoBingAn API 進行分析

    使用 construction-reports/upload/ 端點（與網頁上傳相同）
    使用 JWT Bearer Token 認證

    注意：504 Gateway Timeout 不代表失敗，後端可能仍在處理中
    """
    try:
        # 確保檔名有 .pdf 副檔名（某些 Google Drive 檔案沒有副檔名）
        if not file_name.lower().endswith('.pdf'):
            file_name = file_name + '.pdf'
            print(f"  ℹ️  自動加上 .pdf 副檔名: {file_name}")

        files = {
            'file': (file_name, pdf_content, 'application/pdf')
        }

        # 使用 construction-reports 端點的參數格式
        data = {
            'group_id': GROUP_ID,
            'report_type': 'weekly',  # daily, weekly, monthly, incident, inspection
            'primary_language': 'zh-TW'
        }

        # 設定 JWT 認證標頭（使用自動刷新的 Token）
        valid_token = get_valid_token()
        headers = {
            'Authorization': f'Bearer {valid_token}'
        }

        response = requests.post(
            GEOBINGAN_API_URL,
            files=files,
            data=data,
            headers=headers,
            timeout=600  # 10 分鐘，給後端足夠時間處理 AI 分析
        )

        if response.status_code in [200, 201, 202]:
            result = response.json()
            report_id = result.get('id') or result.get('report_id', 'N/A')
            parse_status = result.get('parse_status', '')

            print(f"  ✅ 上傳成功！", flush=True)
            print(f"     - Report ID: {report_id}", flush=True)
            if parse_status:
                print(f"     - 解析狀態: {parse_status}", flush=True)
            if result.get('message'):
                print(f"     - {result.get('message')}", flush=True)
            return result
        elif response.status_code == 504:
            # 504 Gateway Timeout - 後端可能仍在處理中
            print(f"  ⏳ 已送出，後端處理中（504 Timeout，這是正常的）")
            print(f"     PDF 已成功傳送到伺服器，AI 分析需要 2-5 分鐘")
            print(f"     後端會在背景完成處理，稍後可在系統中查看結果")
            return {
                'status': 'processing',
                'message': 'PDF uploaded, backend processing in background',
                'file_name': file_name,
                'project_code': project_code
            }
        elif response.status_code == 502:
            # 502 Bad Gateway - 類似 504，後端可能仍在處理
            print(f"  ⏳ 已送出，後端處理中（502 Gateway，這是正常的）")
            return {
                'status': 'processing',
                'message': 'PDF uploaded, backend processing in background',
                'file_name': file_name,
                'project_code': project_code
            }
        elif response.status_code == 401:
            # Token 過期，嘗試刷新並重試
            print(f"  ⚠️  Token 已過期，嘗試刷新...")
            new_token = refresh_access_token()
            if new_token:
                # 使用新 Token 重試一次
                headers['Authorization'] = f'Bearer {new_token}'
                retry_response = requests.post(
                    GEOBINGAN_API_URL,
                    files={'file': (file_name, pdf_content, 'application/pdf')},
                    data=data,
                    headers=headers,
                    timeout=600
                )
                if retry_response.status_code in [200, 201, 202]:
                    result = retry_response.json()
                    report_id = result.get('id') or result.get('report_id', 'N/A')
                    print(f"  ✅ 重試成功！Report ID: {report_id}")
                    return result
                else:
                    print(f"  ❌ 重試失敗 ({retry_response.status_code})")
                    return None
            else:
                print(f"  ❌ Token 刷新失敗，無法繼續上傳")
                return None
        else:
            print(f"  ❌ API 錯誤 ({response.status_code}): {response.text[:300]}")
            return None

    except requests.exceptions.Timeout:
        # Client timeout - 但 PDF 可能已經到達伺服器
        print(f"  ⏳ 連線超時，但 PDF 可能已送達伺服器")
        print(f"     後端 AI 分析需要較長時間，請稍後在系統中確認")
        return {
            'status': 'processing',
            'message': 'Connection timeout, but PDF may have been received',
            'file_name': file_name,
            'project_code': project_code
        }
    except Exception as e:
        print(f"  ❌ 上傳失敗: {e}")
        return None


def process_single_pdf(service, pdf: Dict, state: dict, idx: int, total: int) -> Dict:
    """
    處理單個 PDF 的下載和上傳（可用於並行處理）

    Returns:
        Dict with keys: success (bool), pdf (Dict), result (Optional[dict])
    """
    print(f"\n[{idx}/{total}] 處理: {pdf['folder_name']}/{pdf['name']}", flush=True)

    # 下載
    pdf_content = download_pdf(service, pdf['id'], pdf['name'])
    if not pdf_content:
        return {'success': False, 'pdf': pdf, 'result': None, 'error': 'download_failed'}

    # 上傳
    result = upload_to_geobingan(pdf_content, pdf['name'], pdf['folder_name'])

    if result:
        unique_id = f"{pdf['folder_name']}/{pdf['name']}"
        with state_lock:
            state['uploaded_files'].append(unique_id)
            save_state(state)
        # 同時儲存到永久歷史記錄
        add_to_history(unique_id)
        return {'success': True, 'pdf': pdf, 'result': result}
    else:
        with state_lock:
            state['errors'].append({
                'folder': pdf['folder_name'],
                'file': pdf['name'],
                'file_id': pdf['id']
            })
            save_state(state)
        return {'success': False, 'pdf': pdf, 'result': None, 'error': 'upload_failed'}


def main():
    """主程式"""
    print("\n" + "=" * 60)
    print("🚀 上傳最新 5 筆 PDF 到 geoBingAn（效能優化版）")
    if ENABLE_PARALLEL_UPLOAD:
        print(f"   ⚡ 並行上傳模式（{MAX_WORKERS} 執行緒）")
    print("=" * 60)

    # 初始化
    service = get_drive_service()
    state = load_state()

    print(f"\n📊 狀態:")
    print(f"  已上傳: {len(state['uploaded_files'])} 個檔案")
    print(f"  錯誤記錄: {len(state['errors'])} 筆")

    # 列出建案資料夾（使用智慧掃描和快取）
    print(f"\n📁 列出建案資料夾（智慧掃描模式）...")
    project_folders = list_project_folders(service, use_cache=True, state=state, days_ago=DAYS_AGO)

    if not project_folders:
        print("⚠️  未找到最近修改的資料夾，嘗試完整掃描...")
        # 回退：完整掃描（不使用時間過濾）
        try:
            query = "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            results = service.files().list(
                q=query,
                corpora='drive',
                driveId=SHARED_DRIVE_ID,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                pageSize=1000,
                fields='files(id, name, modifiedTime)'
            ).execute()
            project_folders = results.get('files', [])
            print(f"✅ 完整掃描找到 {len(project_folders)} 個資料夾")
        except Exception as e:
            print(f"❌ 完整掃描失敗: {e}")
            sys.exit(0)

    # 收集 PDF（使用快取）
    print(f"\n📄 收集 PDF 檔案...")
    all_pdfs = list_all_pdfs_with_folder_info(service, project_folders, use_cache=True, state=state)

    if not all_pdfs:
        print("❌ 未找到任何 PDF 檔案")
        sys.exit(0)

    # 儲存快取（在開始過濾之前）
    with state_lock:
        save_state(state)

    print(f"✅ 找到 {len(all_pdfs)} 個 PDF 檔案")

    # 計算日期閾值（最近 N 天）
    cutoff_date = datetime.now() - timedelta(days=DAYS_AGO)
    cutoff_date_str = cutoff_date.isoformat() + 'Z'

    print(f"\n🗓️  過濾最近 {DAYS_AGO} 天更新的 PDF（{cutoff_date.strftime('%Y-%m-%d')} 之後）...")

    # 過濾最近 N 天的 PDF
    recent_pdfs = []
    for pdf in all_pdfs:
        # Google Drive 的 modifiedTime 格式: '2025-12-29T06:11:04.237Z'
        if pdf['modifiedTime'] >= cutoff_date_str:
            recent_pdfs.append(pdf)

    print(f"✅ 找到 {len(recent_pdfs)} 個最近 {DAYS_AGO} 天更新的 PDF")

    # 排序：按修改時間降序（最新的在前面）
    recent_pdfs.sort(key=lambda x: x['modifiedTime'], reverse=True)

    # 過濾掉已上傳的和排除清單中的檔案，最多取 MAX_UPLOADS 筆
    pdfs_to_upload = []
    excluded_count = 0
    for pdf in recent_pdfs:
        # 檢查是否在排除清單中
        if pdf['name'] in EXCLUDE_FILES:
            excluded_count += 1
            continue

        unique_id = f"{pdf['folder_name']}/{pdf['name']}"
        if unique_id not in state['uploaded_files']:
            pdfs_to_upload.append(pdf)
            if len(pdfs_to_upload) >= MAX_UPLOADS:
                break

    if excluded_count > 0:
        print(f"⏭️  已跳過 {excluded_count} 個排除清單中的檔案")

    if not pdfs_to_upload:
        print(f"\n⚠️  最新的 {MAX_UPLOADS} 筆 PDF 都已上傳過了！")
        print("\n如要重新上傳，請刪除狀態檔案:")
        print(f"  rm {STATE_FILE}")
        sys.exit(0)

    print(f"\n📋 將上傳以下 {len(pdfs_to_upload)} 個最新 PDF:")
    for i, pdf in enumerate(pdfs_to_upload, 1):
        print(f"{i}. {pdf['folder_name']}/{pdf['name']}")
        print(f"   修改時間: {pdf['modifiedTime']}")

    # 詢問確認
    if AUTO_CONFIRM:
        print(f"\n🤖 自動確認模式：將上傳這 {len(pdfs_to_upload)} 個檔案")
    else:
        response = input(f"\n是否繼續上傳這 {len(pdfs_to_upload)} 個檔案? (y/n): ")
        if response.lower() != 'y':
            print("👋 已取消")
            sys.exit(0)

    # 上傳
    print("\n" + "=" * 60)
    print("開始上傳")
    print("=" * 60)

    success_count = 0
    error_count = 0

    if ENABLE_PARALLEL_UPLOAD:
        # 並行上傳模式
        print(f"⚡ 使用並行上傳（{MAX_WORKERS} 執行緒）")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 提交所有任務
            future_to_pdf = {
                executor.submit(process_single_pdf, service, pdf, state, idx, len(pdfs_to_upload)): pdf
                for idx, pdf in enumerate(pdfs_to_upload, 1)
            }

            # 處理完成的任務
            for future in as_completed(future_to_pdf):
                result = future.result()
                if result['success']:
                    success_count += 1
                else:
                    error_count += 1

                # 速率控制：每個任務完成後稍作延遲
                time.sleep(DELAY_BETWEEN_UPLOADS / MAX_WORKERS)

    else:
        # 序列上傳模式（原有邏輯）
        for idx, pdf in enumerate(pdfs_to_upload, 1):
            result = process_single_pdf(service, pdf, state, idx, len(pdfs_to_upload))

            if result['success']:
                success_count += 1
            else:
                error_count += 1

            # 速率控制：等待避免觸發 API 限制（除了最後一個）
            if idx < len(pdfs_to_upload):
                print(f"  ⏳ 等待 {DELAY_BETWEEN_UPLOADS} 秒（避免 API 速率限制）...", flush=True)
                time.sleep(DELAY_BETWEEN_UPLOADS)

    # 最終統計
    print("\n" + "=" * 60)
    print("📊 上傳完成統計")
    print("=" * 60)
    print(f"✅ 成功上傳: {success_count} 個檔案")
    print(f"❌ 失敗: {error_count} 個檔案")

    if state['errors']:
        print(f"\n❌ 失敗的檔案:")
        for error in state['errors']:
            print(f"  - {error['folder']}/{error['file']}")

    print("\n✅ 測試完成")
    print(f"📝 狀態已儲存到: {STATE_FILE}")
    print("=" * 60)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 使用者中斷執行")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 發生錯誤: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
