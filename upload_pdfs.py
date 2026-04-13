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
import fcntl
import requests
from pathlib import Path
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from typing import Dict, List, Optional
import threading
import re
from filename_date_parser import parse_date_from_filename, FILENAME_DATE_CUTOFF
from jwt_auth import decode_jwt_payload, is_token_expired, refresh_access_token, get_valid_token

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
    try:
        from config import SHARED_DRIVE_ID
    except ImportError:
        SHARED_DRIVE_ID = os.environ.get('SHARED_DRIVE_ID', '0AIvp1h-6BZ1oUk9PVA')
    print(f"✅ 已載入認證配置（用戶: {USER_EMAIL}）", flush=True)
except ImportError as e:
    print("❌ 找不到 config.py 或缺少必要設定")
    print(f"   錯誤: {e}")
    print("   請參考 config.py.example 建立 config.py")
    sys.exit(1)

# 全域變數：當前使用的 Token
current_access_token = JWT_TOKEN

# ================== 設定區域 ==================
# Google Drive 認證
SERVICE_ACCOUNT_FILE = os.environ.get(
    'GOOGLE_CREDENTIALS',
    os.path.join(os.path.dirname(__file__), 'credentials.json')
)
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

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
DELAY_BETWEEN_UPLOADS = 0.5  # 後端為非同步 AI 處理，不需長等待

# 並行上傳設定

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

# 批次寫入計數器：錯誤記錄每 BATCH_SAVE_INTERVAL 次才寫入一次
# 注意：成功上傳記錄（uploaded_files）立即寫入，確保 crash 後不會重複上傳
BATCH_SAVE_INTERVAL = 10
_pending_error_saves = 0  # 追蹤錯誤記錄自上次寫入以來的變更數量


# ================== JWT Token 管理 ==================
# JWT 功能已抽取到 jwt_auth.py 模組，以下為本模組的包裝函數


def update_config_token(new_token: str):
    """
    更新 .env 中的 JWT_TOKEN（透過 config 模組）
    """
    try:
        from config import update_jwt_token
        update_jwt_token(new_token)
        print(f"📝 已更新 .env 中的 Token", flush=True)
    except Exception as e:
        print(f"⚠️  無法更新 Token: {e}", flush=True)


def _get_valid_token() -> str:
    """
    取得有效的 access token（使用 jwt_auth 模組）

    如果當前 token 即將過期，會自動刷新

    Returns:
        有效的 access token
    """
    global current_access_token

    valid_token, was_refreshed = get_valid_token(
        current_access_token, REFRESH_TOKEN, GEOBINGAN_REFRESH_URL
    )
    if was_refreshed:
        current_access_token = valid_token
        update_config_token(valid_token)

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
    """載入已上傳的 PDF 記錄（包含快取）

    自動合併 upload_history_all.json（git 追蹤）的上傳記錄，
    確保 fresh clone 後不會重複上傳已處理的檔案。
    """
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
    else:
        state = {
            'uploaded_files': [],
            'errors': [],
            'cache': {
                'folders': [],
                'pdfs': [],
                'last_scan': None
            }
        }

    # 合併 git 追蹤的上傳歷史（確保 fresh clone 不重複上傳）
    history = load_history()
    history_files = set(history.get('uploaded_files', []))
    state_files = set(state.get('uploaded_files', []))
    merged = state_files | history_files
    if len(merged) > len(state_files):
        state['uploaded_files'] = list(merged)
        print(f"📂 已合併上傳歷史: {len(history_files)} 筆（本地 {len(state_files)} + 歷史 {len(history_files)} → {len(merged)} 筆）", flush=True)

    return state


STATE_LOCK_FILE = STATE_FILE + '.lock'


def save_state(state: dict):
    """儲存已上傳的 PDF 記錄（跨 process 安全）

    使用 flock 檔案鎖 + read-merge-write + atomic replace：
    1. 取得檔案鎖（排他），阻擋其他 process 同時寫入
    2. 重新讀取磁碟上的最新 state，合併本 process 的新增項
    3. 寫入 PID 暫存檔再 os.replace()，防止寫到一半損壞
    注意：此函數不包含 thread lock，呼叫者需要自行管理 state_lock
    """
    state_dir = os.path.dirname(STATE_FILE)
    os.makedirs(state_dir, exist_ok=True)

    lock_fd = open(STATE_LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # 讀取磁碟上的最新 state，合併本 process 的新增項
        disk_state = None
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    disk_state = json.load(f)
            except (json.JSONDecodeError, IOError):
                disk_state = None

        if disk_state is not None:
            # 合併 uploaded_files：取聯集
            disk_uploaded = set(disk_state.get('uploaded_files', []))
            mem_uploaded = set(state.get('uploaded_files', []))
            state['uploaded_files'] = list(disk_uploaded | mem_uploaded)

            # 合併 errors：以 (folder, file) 為 key 去重
            disk_errors = {(e.get('folder'), e.get('file')): e for e in disk_state.get('errors', [])}
            for e in state.get('errors', []):
                disk_errors[(e.get('folder'), e.get('file'))] = e
            state['errors'] = list(disk_errors.values())

        # 原子寫入
        tmp_file = f"{STATE_FILE}.tmp.{os.getpid()}"
        try:
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(state, indent=2, ensure_ascii=False, fp=f)
            os.replace(tmp_file, STATE_FILE)
        except Exception:
            try:
                os.unlink(tmp_file)
            except OSError:
                pass
            raise
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


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
    列出 Shared Drive 中所有 PDF，並附加資料夾資訊（支援快取）

    使用單一 Drive 查詢掃描整個 Shared Drive 的 PDF（分頁），
    再用 folder lookup table 對應資料夾名稱。
    比逐資料夾查詢（1000 次 API 呼叫）快 10-20 倍。

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

    # 建立 folder_id → folder_name 的查找表
    folder_lookup = {f['id']: f['name'] for f in folders}

    # 單一查詢掃描整個 Shared Drive 的所有 PDF（分頁處理）
    print(f"🔍 掃描 Shared Drive 中的所有 PDF...")
    all_pdfs = []
    page_token = None
    page_count = 0

    while True:
        try:
            query = "mimeType = 'application/pdf' and trashed = false"
            results = service.files().list(
                q=query,
                corpora='drive',
                driveId=SHARED_DRIVE_ID,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                pageSize=1000,
                pageToken=page_token,
                fields='nextPageToken, files(id, name, size, modifiedTime, parents)'
            ).execute()

            pdfs = results.get('files', [])
            page_count += 1
            print(f"  第 {page_count} 頁: {len(pdfs)} 個 PDF")

            for pdf in pdfs:
                # 從 parents 找到對應的資料夾名稱
                parents = pdf.get('parents', [])
                folder_id = parents[0] if parents else None
                folder_name = folder_lookup.get(folder_id, '')

                if folder_name:
                    pdf['folder_id'] = folder_id
                    pdf['folder_name'] = folder_name
                    # 移除 parents 欄位（不需要存入快取）
                    pdf.pop('parents', None)
                    all_pdfs.append(pdf)

            page_token = results.get('nextPageToken')
            if not page_token:
                break

        except HttpError as e:
            print(f"  ❌ 批次掃描第 {page_count + 1} 頁失敗: {e}")
            print(f"  ⚠️  丟棄部分結果，改為逐資料夾完整掃描...")
            # 批次掃描中斷，無法確定哪些資料夾完整掃到，
            # 全部丟棄改用逐資料夾查詢確保完整性
            all_pdfs = []
            for idx, folder in enumerate(folders, 1):
                if idx % 50 == 0:
                    print(f"    回退進度: {idx}/{len(folders)} 個資料夾...")
                try:
                    fallback_query = (
                        f"'{folder['id']}' in parents and "
                        f"mimeType = 'application/pdf' and trashed = false"
                    )
                    fallback_results = service.files().list(
                        q=fallback_query,
                        pageSize=1000,
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                        fields='files(id, name, size, modifiedTime)'
                    ).execute()
                    for pdf in fallback_results.get('files', []):
                        pdf['folder_id'] = folder['id']
                        pdf['folder_name'] = folder['name']
                        all_pdfs.append(pdf)
                except HttpError:
                    continue
            break

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


def upload_to_geobingan(pdf_content: bytes, file_name: str, project_code: str,
                        max_retries: int = 3) -> Optional[dict]:
    """
    上傳 PDF 到 geoBingAn API 進行分析（含重試機制）

    使用 construction-reports/upload/ 端點（與網頁上傳相同）
    使用 JWT Bearer Token 認證

    重試策略：
    - 503 Service Unavailable：伺服器暫時不可用，安全重試
    - 502/504 Gateway Timeout：PDF 可能已送達後端，不重試（避免重複上傳）
    - Connection timeout：網路層逾時，安全重試（request body 未必送達）
    """
    # 確保檔名有 .pdf 副檔名（某些 Google Drive 檔案沒有副檔名）
    if not file_name.lower().endswith('.pdf'):
        file_name = file_name + '.pdf'
        print(f"  ℹ️  自動加上 .pdf 副檔名: {file_name}")

    # 使用 construction-reports 端點的參數格式
    data = {
        'group_id': GROUP_ID,
        'report_type': 'weekly',  # daily, weekly, monthly, incident, inspection
        'primary_language': 'zh-TW'
    }

    retry_delays = [5, 15, 30]  # 指數退避延遲（秒）

    for attempt in range(max_retries):
        try:
            files = {
                'file': (file_name, pdf_content, 'application/pdf')
            }

            # 設定 JWT 認證標頭（使用自動刷新的 Token）
            valid_token = _get_valid_token()
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
            elif response.status_code in [502, 504]:
                # 502/504: PDF 可能已送達後端，不重試避免重複上傳
                print(f"  ⏳ 已送出，後端處理中（{response.status_code}）")
                print(f"     PDF 可能已成功傳送到伺服器，AI 分析需要 2-5 分鐘")
                return {
                    'status': 'processing',
                    'message': 'PDF uploaded, backend processing in background',
                    'file_name': file_name,
                    'project_code': project_code
                }
            elif response.status_code == 503:
                # 503: 伺服器暫時不可用，安全重試
                if attempt < max_retries - 1:
                    delay = retry_delays[attempt]
                    print(f"  ⚠️  伺服器暫時不可用 (503)，第 {attempt + 1}/{max_retries} 次重試，等待 {delay} 秒...", flush=True)
                    time.sleep(delay)
                    continue
                else:
                    print(f"  ❌ 伺服器不可用 (503)，已重試 {max_retries} 次")
                    return None
            elif response.status_code == 401:
                # Token 過期，嘗試刷新並重試
                print(f"  ⚠️  Token 已過期，嘗試刷新...")
                new_token = refresh_access_token(REFRESH_TOKEN, GEOBINGAN_REFRESH_URL)
                if new_token:
                    global current_access_token
                    current_access_token = new_token
                    update_config_token(new_token)
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

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            # 連線超時或連線錯誤 - 重試
            if attempt < max_retries - 1:
                delay = retry_delays[attempt]
                print(f"  ⚠️  {type(e).__name__}，第 {attempt + 1}/{max_retries} 次重試，等待 {delay} 秒...", flush=True)
                time.sleep(delay)
                continue
            else:
                # 最後一次嘗試仍失敗
                print(f"  ⏳ 連線超時（已重試 {max_retries} 次），但 PDF 可能已送達伺服器")
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

    return None


def process_single_pdf(service, pdf: Dict, state: dict, idx: int, total: int) -> Dict:
    """
    處理單個 PDF 的下載和上傳（可用於並行處理）

    狀態檔案採批次寫入策略：每 BATCH_SAVE_INTERVAL 次變更才寫入一次，
    減少大型狀態檔案的 I/O 次數。呼叫者應在所有處理完成後呼叫
    flush_state() 確保最後的變更被寫入。

    Returns:
        Dict with keys: success (bool), pdf (Dict), result (Optional[dict])
    """
    global _pending_error_saves

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
            # 成功上傳立即寫入，確保 crash 後不會重複上傳
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
            # 錯誤記錄批次寫入（遺失不影響冪等性）
            _pending_error_saves += 1
            if _pending_error_saves >= BATCH_SAVE_INTERVAL:
                save_state(state)
                _pending_error_saves = 0
        return {'success': False, 'pdf': pdf, 'result': None, 'error': 'upload_failed'}


def flush_state(state: dict):
    """寫入所有待處理的錯誤記錄（批次寫入的最終 flush）"""
    global _pending_error_saves
    with state_lock:
        if _pending_error_saves > 0:
            save_state(state)
            _pending_error_saves = 0


def main():
    """主程式"""
    print("\n" + "=" * 60)
    print("🚀 上傳最新 PDF 到 geoBingAn")
    print("=" * 60)

    # 初始化
    service = get_drive_service()
    state = load_state()

    print(f"\n📊 狀態:")
    print(f"  已上傳: {len(state['uploaded_files'])} 個檔案")
    print(f"  錯誤記錄: {len(state['errors'])} 筆")

    # 列出所有建案資料夾（完整掃描，因為過濾依據是檔名日期而非資料夾修改時間）
    print(f"\n📁 列出所有建案資料夾...")
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
        print(f"✅ 找到 {len(project_folders)} 個資料夾")
    except Exception as e:
        print(f"❌ 掃描資料夾失敗: {e}")
        sys.exit(1)

    if not project_folders:
        print("⚠️  未找到任何資料夾")
        sys.exit(1)

    # 收集 PDF（使用快取加速，但若快取中的資料夾數量與本次掃描不符則重建）
    print(f"\n📄 收集 PDF 檔案...")
    cached_folder_count = len(state.get('cache', {}).get('folders', []))
    if cached_folder_count > 0 and cached_folder_count < len(project_folders):
        print(f"⚠️  快取中只有 {cached_folder_count} 個資料夾的 PDF（本次掃描 {len(project_folders)} 個），清除快取重新掃描")
        state['cache']['pdfs'] = []
        state['cache']['last_scan'] = None
    all_pdfs = list_all_pdfs_with_folder_info(service, project_folders, use_cache=True, state=state)

    if not all_pdfs:
        print("❌ 未找到任何 PDF 檔案")
        sys.exit(1)

    # 儲存快取（在開始過濾之前）
    with state_lock:
        save_state(state)

    print(f"✅ 找到 {len(all_pdfs)} 個 PDF 檔案")

    # 使用檔名日期過濾（農曆新年 2026-02-17 之後）
    cutoff_date = FILENAME_DATE_CUTOFF
    print(f"\n🗓️  過濾檔名日期在 {cutoff_date.strftime('%Y-%m-%d')}（農曆新年初一）之後的 PDF...")

    recent_pdfs = []
    no_date_count = 0
    too_old_count = 0
    for pdf in all_pdfs:
        # 嘗試從檔名解析日期，若失敗則用 folder_name + 檔名組合再試
        file_date = parse_date_from_filename(pdf['name'])
        if file_date is None and pdf.get('folder_name'):
            file_date = parse_date_from_filename(pdf['folder_name'] + '/' + pdf['name'])
        if file_date is None:
            no_date_count += 1
            continue
        if file_date > cutoff_date:
            pdf['_parsed_date'] = file_date
            recent_pdfs.append(pdf)
        else:
            too_old_count += 1

    print(f"✅ 找到 {len(recent_pdfs)} 個農曆新年後的 PDF")
    if no_date_count > 0:
        print(f"⏭️  {no_date_count} 個無法從檔名解析日期（已跳過）")
    if too_old_count > 0:
        print(f"⏭️  {too_old_count} 個日期在農曆新年之前（已跳過）")

    # 排序：按檔名解析日期降序（最新的在前面）
    recent_pdfs.sort(key=lambda x: x.get('_parsed_date', datetime.min), reverse=True)

    # 移除暫存的 datetime 物件，避免 JSON 序列化失敗
    for pdf in recent_pdfs:
        pdf.pop('_parsed_date', None)

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

    # 寫入所有剩餘的狀態變更
    flush_state(state)

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
