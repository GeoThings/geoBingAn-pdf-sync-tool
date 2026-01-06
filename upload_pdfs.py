#!/usr/bin/env python3
"""
上傳最新 5 筆 PDF 到 geoBingAn 分析工具（效能優化版本）

功能：
1. 掃描 Google Drive Shared Drive 中的建案 PDF
2. 只上傳最新的 5 個 PDF 檔案
3. 記錄已上傳的 PDF，避免重複處理
4. 使用 JWT 認證（jerryjo0802@gmail.com）
5. 【新增】並行上傳支援（可選）
"""
import json
import os
import sys
import io
import time
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
        GEOBINGAN_API_URL
    )
    print(f"✅ 已載入認證配置（用戶: {USER_EMAIL}）")
except ImportError:
    print("❌ 找不到 config.py，請先建立配置檔案")
    print("   請參考 config.py.example 建立 config.py")
    sys.exit(1)

# ================== 設定區域 ==================
# Google Drive 認證
SERVICE_ACCOUNT_FILE = '/Users/geothingsmacbookair/Downloads/credentials.json'
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
SHARED_DRIVE_ID = '0AIvp1h-6BZ1oUk9PVA'

# geoBingAn API 設定（從 config.py 匯入）
# GEOBINGAN_API_URL - 已從 config.py 匯入
GEOBINGAN_SCENARIO_ID = 'construction_safety_pdf'
GEOBINGAN_LANGUAGE = 'zh-TW'

# 狀態追蹤檔案
STATE_FILE = './state/uploaded_to_geobingan_7days.json'

# 日期過濾設定
DAYS_AGO = 7  # 只上傳最近 7 天更新的 PDF

# 批次上傳設定
MAX_UPLOADS = 500  # 最多上傳 500 筆 PDF（支援完整 7 天上傳）

# 速率控制：每次上傳之間的延遲（秒）
DELAY_BETWEEN_UPLOADS = 5  # 優化：從 20 秒減少到 5 秒

# 並行上傳設定
ENABLE_PARALLEL_UPLOAD = False  # 設為 True 啟用並行上傳（實驗性功能）
MAX_WORKERS = 3  # 並行上傳的最大執行緒數

# 自動確認（測試模式）
AUTO_CONFIRM = True  # 啟用自動確認進行批次上傳
# ============================================

# 全域鎖，用於並行上傳時保護狀態檔案
state_lock = threading.Lock()


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
    """儲存已上傳的 PDF 記錄（執行緒安全版本）"""
    with state_lock:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, indent=2, ensure_ascii=False, fp=f)


def get_drive_service():
    """初始化 Google Drive API（Service Account）"""
    print("🔑 初始化 Google Drive API (Service Account)")

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        print(f"❌ 找不到 Service Account 金鑰: {SERVICE_ACCOUNT_FILE}")
        sys.exit(1)

    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)

    service = build('drive', 'v3', credentials=credentials)
    print(f"✅ 已初始化 ({credentials.service_account_email})")
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
            print(f"  📥 已下載: {file_name} ({len(content)} bytes)")
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

    使用 JWT Bearer Token 認證
    """
    try:
        files = {
            'file': (file_name, pdf_content, 'application/pdf')
        }

        data = {
            'scenario_id': GEOBINGAN_SCENARIO_ID,
            'language': GEOBINGAN_LANGUAGE,
            'save_to_report': True,
            'group_id': GROUP_ID,  # 必填：群組 ID
            'additional_context': f'建案代碼: {project_code}'
        }

        # 設定 JWT 認證標頭
        headers = {
            'Authorization': f'Bearer {JWT_TOKEN}'
        }

        response = requests.post(
            GEOBINGAN_API_URL,
            files=files,
            data=data,
            headers=headers,  # 加入認證標頭
            timeout=300
        )

        if response.status_code == 200:
            result = response.json()
            # 即使 API 返回 success=False，我們也檢查是否有建立建案記錄
            report_id = result.get('report_id', 'N/A')
            construction_project = result.get('construction_project')

            if construction_project:
                print(f"  ✅ 建案建立成功")
                print(f"     - Report ID: {report_id}")
                print(f"     - 建案代碼: {construction_project.get('project_code')}")
                print(f"     - 監測報告ID: {construction_project.get('monitoring_report_id')}")
                return result
            elif result.get('success'):
                print(f"  ✅ 分析成功，Report ID: {report_id}")
                return result
            else:
                print(f"  ⚠️  分析失敗: {result.get('error', 'Unknown error')}")
                # 即使有錯誤訊息，仍然返回結果（因為建案可能已建立）
                return result
        else:
            print(f"  ❌ API 錯誤 ({response.status_code}): {response.text[:200]}")
            return None

    except requests.exceptions.Timeout:
        print(f"  ⏱️  上傳超時（5 分鐘）")
        return None
    except Exception as e:
        print(f"  ❌ 上傳失敗: {e}")
        return None


def process_single_pdf(service, pdf: Dict, state: dict, idx: int, total: int) -> Dict:
    """
    處理單個 PDF 的下載和上傳（可用於並行處理）

    Returns:
        Dict with keys: success (bool), pdf (Dict), result (Optional[dict])
    """
    print(f"\n[{idx}/{total}] 處理: {pdf['folder_name']}/{pdf['name']}")

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

    # 過濾掉已上傳的，最多取 MAX_UPLOADS 筆
    pdfs_to_upload = []
    for pdf in recent_pdfs:
        unique_id = f"{pdf['folder_name']}/{pdf['name']}"
        if unique_id not in state['uploaded_files']:
            pdfs_to_upload.append(pdf)
            if len(pdfs_to_upload) >= MAX_UPLOADS:
                break

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
                print(f"  ⏳ 等待 {DELAY_BETWEEN_UPLOADS} 秒（避免 API 速率限制）...")
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
