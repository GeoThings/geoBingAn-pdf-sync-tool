#!/usr/bin/env python3
"""
建築執照監測資料同步工具 v5.0 (智慧分塊版)
核心升級：
1. [智慧分塊演算法] 改用「領地搜尋法」解析 PDF：
   - 先定位所有建照號碼的位置。
   - 在兩個號碼之間的區域搜尋「任何」Google Drive 連結。
   - 自動修復像 112-0238 這種因網址格式特殊而漏抓的建案。
2. [容錯率提升] 不再依賴嚴格的網址 Regex，大幅降低漏抓機率。
3. [功能保留] 包含隨機跳查、斷點續傳、自動建立資料夾等所有功能。
"""
import json
import os
import re
import requests
import urllib3
import time
import io
import sys
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
import PyPDF2
from typing import Dict, List, Tuple

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================== 設定區域 ==================
# 請確認金鑰路徑是否正確
SERVICE_ACCOUNT_FILE = os.environ.get(
    'GOOGLE_CREDENTIALS',
    os.path.join(os.path.dirname(__file__), 'credentials.json')
)
SCOPES = ['https://www.googleapis.com/auth/drive']

# 從 config.py 匯入 Shared Drive ID
try:
    from config import SHARED_DRIVE_ID
except ImportError:
    SHARED_DRIVE_ID = os.environ.get('SHARED_DRIVE_ID', '0AIvp1h-6BZ1oUk9PVA')
PDF_LIST_URL = 'https://www-ws.gov.taipei/001/Upload/845/relfile/-1/845/03b35db7-a123-4b29-b881-1cb17fa9c4f2.pdf'
STATE_FILE = './state/sync_permits_progress.json'
# ============================================

# Google Drive API 認證
# credentials 是 thread-safe 的，但 httplib2.Http 不是。
# 每個 thread 需要自己的 service instance。
# https://googleapis.github.io/google-api-python-client/docs/thread_safety.html
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)

# 主執行緒的 service（用於 run() 中的非並行操作）
drive_service = build('drive', 'v3', credentials=credentials)

# Thread-local storage
_thread_local = threading.local()


def get_thread_drive_service():
    """取得當前 thread 的獨立 Drive service instance"""
    if not hasattr(_thread_local, 'service'):
        _thread_local.service = build('drive', 'v3', credentials=credentials)
    return _thread_local.service

# 並行處理設定
MAX_CONCURRENT_PERMITS = 5  # 同時處理的建案數（Google Drive API quota: 12,000 req/min）


class PermitSync:
    def __init__(self):
        self.target_folders = {}
        self.permit_mapping = {}
        self.state = self.load_state()
        self.restricted_files = []
        self._state_lock = threading.Lock()
        self._print_lock = threading.Lock()
        # 效能快取：目標資料夾的檔案樹和子資料夾 ID
        self._target_file_cache = {}   # permit_no → set of "path/filename" or "filename"
        self._subfolder_cache = {}     # (parent_id, subfolder_name) → folder_id
        
    def load_state(self) -> dict:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
            # 向後相容：舊版 processed 是 list，新版是 dict
            if isinstance(state.get('processed'), list):
                state['processed'] = {p: '' for p in state['processed']}
            return state
        return {'processed': {}, 'skipped': [], 'errors': [], 'restricted': []}
    
    def save_state(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with self._state_lock:
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.state, indent=2, ensure_ascii=False, fp=f)

    def _print(self, msg: str):
        """Thread-safe print"""
        with self._print_lock:
            print(msg, flush=True)
    
    def download_pdf_list(self) -> str:
        print("📥 下載建案列表 PDF...")
        try:
            response = requests.get(PDF_LIST_URL, verify=False, timeout=30)
            pdf_path = '/tmp/permit_list.pdf'
            with open(pdf_path, 'wb') as f:
                f.write(response.content)
            print(f"✅ 列表已下載: {len(response.content)} bytes")
            return pdf_path
        except Exception as e:
            print(f"❌ 下載失敗: {e}")
            sys.exit(1)
    
    def parse_pdf_list(self, pdf_path: str) -> Dict[str, str]:
        print("\n📖 解析 PDF 列表 (智慧分塊演算法)...")
        with open(pdf_path, 'rb') as f:
            pdf_reader = PyPDF2.PdfReader(f)
            all_text = ""
            for page in pdf_reader.pages:
                text = page.extract_text()
                if text: all_text += text
        
        # 移除空白，接合斷行
        clean_text = re.sub(r'\s+', '', all_text)
        permit_mapping = {}

        # 步驟 1: 找出所有「建照號碼」的位置 (插旗)
        # 使用 iterator 記錄每一個 match 的 start/end 位置
        permit_matches = list(re.finditer(r'(\d{2,3}建字第\d{3,5}號)', clean_text))
        
        if not permit_matches:
            print("❌ 錯誤: 未找到任何建照號碼，請檢查 PDF 內容")
            return {}

        count_found = 0
        count_missed = 0

        # 步驟 2: 遍歷每個建案，搜尋它「領地」內的網址
        for i in range(len(permit_matches)):
            current_match = permit_matches[i]
            permit_no = current_match.group(1)
            
            # 定義搜尋範圍 (Chunking)
            # 起點：當前建号的结束位置
            start_pos = current_match.end()
            
            # 終點：下一個建号的開始位置 (如果是最後一個，則搜到字串結尾)
            if i < len(permit_matches) - 1:
                end_pos = permit_matches[i+1].start()
            else:
                end_pos = len(clean_text)
            
            # 提取這段區域的文字
            chunk_text = clean_text[start_pos:end_pos]
            
            # 步驟 3: 在區域內搜尋 Google Drive 連結
            # 這裡使用寬鬆的 Regex，只要是 https://drive.google.com 開頭都抓
            # 並抓取直到遇到非 URL 安全字符 (防止抓到下一個欄位的中文)
            url_match = re.search(r'(https://drive\.google\.com[a-zA-Z0-9/._?=%&-]+)', chunk_text)
            
            if url_match:
                url = url_match.group(1)
                permit_mapping[permit_no] = url
                count_found += 1
            else:
                # 只有當找不到 Google 連結時，才嘗試找 OneDrive (選配)
                onedrive_match = re.search(r'(https://(?:1drv\.ms|onedrive\.live\.com)[\w/._?=%&-]+)', chunk_text)
                if onedrive_match:
                     # 暫時只支援識別，不支援下載 OneDrive (需 Azure 驗證)
                     print(f"  ⚠️ 跳過 OneDrive 連結: {permit_no}")
                else:
                     # print(f"  ⚠️ 無連結: {permit_no}") # 除錯用
                     count_missed += 1

        print(f"✅ 解析完成: 成功配對 {len(permit_mapping)} 個 (無連結/無效: {count_missed} 個)")
        
        # 檢查 0238 是否自動修復 (Debug)
        if '112建字第0238號' in permit_mapping:
            print(f"  ✨ 自動修復檢測: 112建字第0238號 已成功抓取！")
        
        return permit_mapping
    
    def scan_shared_drive(self) -> Dict[str, str]:
        print(f"\n📂 掃描共享雲端...")
        folders = {}
        page_token = None
        while True:
            try:
                results = drive_service.files().list(
                    q=f"'{SHARED_DRIVE_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                    fields='nextPageToken, files(id, name)',
                    pageSize=1000, pageToken=page_token,
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                    corpora='drive', driveId=SHARED_DRIVE_ID
                ).execute()
                for item in results.get('files', []):
                    folders[item['name']] = item['id']
                page_token = results.get('nextPageToken')
                if not page_token: break
            except HttpError:
                break
        return folders
    
    def extract_folder_id_from_url(self, url: str) -> str:
        # 支援 /folders/ID 和 /open?id=ID 兩種格式
        match = re.search(r'/folders/([a-zA-Z0-9_-]+)', url)
        if match: return match.group(1)
        
        match_id = re.search(r'id=([a-zA-Z0-9_-]+)', url)
        if match_id: return match_id.group(1)
        
        return None
    
    def list_files_recursive(self, folder_id: str, path: str = "") -> List[Tuple[str, str, str, str]]:
        files = []
        try:
            query = f"'{folder_id}' in parents and trashed=false"
            results = self._get_svc().files().list(
                q=query, fields='files(id, name, mimeType, webViewLink)',
                pageSize=1000, supportsAllDrives=True, includeItemsFromAllDrives=True
            ).execute()
            
            for item in results.get('files', []):
                item_path = f"{path}/{item['name']}" if path else item['name']
                if item['mimeType'] == 'application/vnd.google-apps.folder':
                    subfolder_files = self.list_files_recursive(item['id'], item_path)
                    files.extend(subfolder_files)
                elif item['mimeType'] == 'application/pdf':
                    files.append((item['id'], item['name'], path, item.get('webViewLink', '')))
        except HttpError:
            pass 
        return files
    
    def preload_target_files(self, folder_id: str, permit_no: str):
        """一次遞迴載入目標資料夾的所有檔名到記憶體 set。
        後續用 set lookup 取代逐檔 API 查詢。
        如果遞迴過程中任何一層失敗，不寫入快取，
        check_file_exists 會回退到逐檔 API 查詢。
        """
        file_set = set()
        if self._preload_recursive(folder_id, "", file_set):
            self._target_file_cache[permit_no] = file_set
        else:
            # 預載入不完整，不使用快取，強制走 API fallback
            self._target_file_cache.pop(permit_no, None)

    def _preload_recursive(self, folder_id: str, path: str, file_set: set) -> bool:
        """遞迴收集資料夾內所有檔案的 path/name。
        回傳 True 表示完整掃描成功，False 表示任一層失敗。
        """
        try:
            page_token = None
            while True:
                results = self._get_svc().files().list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields='nextPageToken, files(id, name, mimeType)',
                    pageSize=1000, pageToken=page_token,
                    supportsAllDrives=True, includeItemsFromAllDrives=True
                ).execute()
                for item in results.get('files', []):
                    item_path = f"{path}/{item['name']}" if path else item['name']
                    if item['mimeType'] == 'application/vnd.google-apps.folder':
                        self._subfolder_cache[(folder_id, item['name'])] = item['id']
                        if not self._preload_recursive(item['id'], item_path, file_set):
                            return False  # 子資料夾失敗，整體失敗
                    else:
                        file_set.add(item_path)
                page_token = results.get('nextPageToken')
                if not page_token:
                    break
            return True
        except HttpError:
            return False

    def check_file_exists(self, folder_id: str, filename: str, path: str = "", permit_no: str = "") -> bool:
        """用預載入的 set 比對檔案是否存在（O(1) lookup，無 API 呼叫）"""
        if permit_no and permit_no in self._target_file_cache:
            key = f"{path}/{filename}" if path else filename
            # 同時檢查 .url 捷徑
            return key in self._target_file_cache[permit_no] or \
                   f"{key}.url" in self._target_file_cache[permit_no]
        # fallback：快取未載入時用 API 查詢
        target_folder_id = folder_id
        if path:
            target_folder_id = self.get_or_create_subfolder(folder_id, path)
            if not target_folder_id: return False
        try:
            query = f"'{target_folder_id}' in parents and (name='{filename}' or name='{filename}.url') and trashed=false"
            results = self._get_svc().files().list(
                q=query, fields='files(id)', supportsAllDrives=True, includeItemsFromAllDrives=True
            ).execute()
            return len(results.get('files', [])) > 0
        except HttpError:
            return False
    
    def create_target_folder(self, folder_name: str) -> str:
        try:
            file_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [SHARED_DRIVE_ID]}
            folder = drive_service.files().create(body=file_metadata, fields='id', supportsAllDrives=True).execute()
            print(f"🆕 已自動建立資料夾: {folder_name}")
            return folder['id']
        except HttpError:
            return None

    def get_or_create_subfolder(self, parent_id: str, path: str) -> str:
        current_folder_id = parent_id
        for folder_name in path.split('/'):
            if not folder_name: continue
            # 先查快取
            cache_key = (current_folder_id, folder_name)
            if cache_key in self._subfolder_cache:
                current_folder_id = self._subfolder_cache[cache_key]
                continue
            try:
                query = f"'{current_folder_id}' in parents and name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
                results = self._get_svc().files().list(q=query, fields='files(id)', supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
                items = results.get('files', [])
                if items:
                    current_folder_id = items[0]['id']
                else:
                    file_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [current_folder_id]}
                    folder = self._get_svc().files().create(body=file_metadata, fields='id', supportsAllDrives=True).execute()
                    current_folder_id = folder['id']
                # 寫入快取
                self._subfolder_cache[cache_key] = current_folder_id
            except HttpError:
                return None
        return current_folder_id

    def create_shortcut_file(self, parent_id: str, filename: str, web_link: str):
        try:
            link_filename = f"{filename}.url"
            file_content = f"[InternetShortcut]\nURL={web_link}"
            file_metadata = {'name': link_filename, 'parents': [parent_id], 'mimeType': 'text/plain'}
            media = MediaIoBaseUpload(io.BytesIO(file_content.encode('utf-8')), mimetype='text/plain', resumable=True)
            self._get_svc().files().create(body=file_metadata, media_body=media, fields='id', supportsAllDrives=True).execute()
            return True
        except Exception:
            return False

    def copy_file(self, source_file_id: str, target_folder_id: str, filename: str, path: str = ""):
        final_folder_id = target_folder_id
        if path:
            final_folder_id = self.get_or_create_subfolder(target_folder_id, path)
            if not final_folder_id: return None, None

        svc = self._get_svc()
        try:
            file_metadata = {'name': filename, 'parents': [final_folder_id]}
            copied_file = svc.files().copy(fileId=source_file_id, body=file_metadata, fields='id', supportsAllDrives=True).execute()
            return copied_file['id'], final_folder_id
        except HttpError as e:
            try:
                request = svc.files().get_media(fileId=source_file_id, supportsAllDrives=True)
                file_buffer = io.BytesIO()
                downloader = MediaIoBaseDownload(file_buffer, request)
                done = False
                while not done: status, done = downloader.next_chunk()
                file_buffer.seek(0)
                media = MediaIoBaseUpload(file_buffer, mimetype='application/pdf', resumable=True)
                uploaded_file = svc.files().create(
                    body={'name': filename, 'parents': [final_folder_id], 'mimeType': 'application/pdf'},
                    media_body=media, fields='id', supportsAllDrives=True).execute()
                return uploaded_file['id'], final_folder_id
            except HttpError as download_error:
                if 'cannotDownloadFile' in str(download_error) or 'cannotCopyFile' in str(e):
                    return 'restricted', final_folder_id
                return None, None

    def _get_svc(self):
        """取得當前 thread 的 Drive service（並行時用 thread-local，序列時用全域）"""
        return get_thread_drive_service()

    def sync_permit(self, permit_no: str, source_url: str, target_folder_id: str):
        self._print(f"\n🔄 監測建案: {permit_no}")
        source_folder_id = self.extract_folder_id_from_url(source_url)
        if not source_folder_id:
            with self._state_lock:
                self.state['errors'].append({'permit': permit_no, 'error': 'Invalid URL ID'})
            return

        try:
            files = self.list_files_recursive(source_folder_id)
            if not files:
                self._print(f"  ⚠️ 來源無檔案")
                return

            # 預載入目標資料夾的完整檔案樹（1 次遞迴 vs 逐檔 API 查詢）
            self.preload_target_files(target_folder_id, permit_no)

            copied = 0

            for file_id, filename, path, web_link in files:
                display_path = f"{path}/{filename}" if path else filename

                if self.check_file_exists(target_folder_id, filename, path, permit_no):
                    continue

                result_id, final_folder_id = self.copy_file(file_id, target_folder_id, filename, path)
                if result_id == 'restricted':
                    self._print(f"    🔒 受限: {display_path} (建立捷徑)")
                    self.create_shortcut_file(final_folder_id, filename, web_link)
                    with self._state_lock:
                        self.restricted_files.append({'filename': filename, 'permit': permit_no})
                    if permit_no in self._target_file_cache:
                        key = f"{path}/{filename}.url" if path else f"{filename}.url"
                        self._target_file_cache[permit_no].add(key)
                elif result_id:
                    self._print(f"    🆕 發現新檔並複製: {display_path}")
                    copied += 1
                    if permit_no in self._target_file_cache:
                        key = f"{path}/{filename}" if path else filename
                        self._target_file_cache[permit_no].add(key)

            if copied > 0:
                self._print(f"  📊 更新完成: 新增 {copied} 個")

            with self._state_lock:
                self.state['processed'][permit_no] = True
            self.save_state()

        except Exception as e:
            self._print(f"  ❌ 處理中斷: {e}")
            with self._state_lock:
                self.state['errors'].append({'permit': permit_no, 'error': str(e)})
            self.save_state()
    
    def run(self):
        print("="*70)
        print(f"🚀 建築執照監測資料同步工具 v5.1 (效能優化版)")
        print("   特性: 增量同步、跳過已處理建案、快速模式")
        print("="*70)

        pdf_path = self.download_pdf_list()
        self.permit_mapping = self.parse_pdf_list(pdf_path)
        self.target_folders = self.scan_shared_drive()

        permit_list = list(self.permit_mapping.items())
        # 隨機打亂，確保每次執行檢查不同建案
        random.shuffle(permit_list)

        # 過濾掉已處理且無錯誤的建案（增量同步）
        unprocessed_permits = []
        for permit_no, source_url in permit_list:
            if permit_no in self.state['processed']:
                has_error = any(e.get('permit') == permit_no for e in self.state.get('errors', []))
                if not has_error:
                    continue
            unprocessed_permits.append((permit_no, source_url))

        print(f"\n📋 監測目標: {len(permit_list)} 個建案")
        print(f"✅ 已處理: {len(permit_list) - len(unprocessed_permits)} 個")
        print(f"🔄 待處理: {len(unprocessed_permits)} 個")

        # 先確保所有建案都有目標資料夾（序列化，因為涉及建立資料夾）
        permits_with_targets = []
        for permit_no, source_url in unprocessed_permits:
            if permit_no in self.target_folders:
                target_id = self.target_folders[permit_no]
            else:
                print(f"\n🔧 發現新建案: {permit_no}")
                target_id = self.create_target_folder(permit_no)
                if target_id:
                    self.target_folders[permit_no] = target_id
                else:
                    continue
            permits_with_targets.append((permit_no, source_url, target_id))

        # 並行處理各建案（每個建案的來源/目標資料夾互相獨立）
        if len(permits_with_targets) > 1:
            print(f"\n⚡ 並行處理（{MAX_CONCURRENT_PERMITS} 執行緒）")
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PERMITS) as executor:
            futures = {
                executor.submit(self.sync_permit, pn, url, tid): pn
                for pn, url, tid in permits_with_targets
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    permit_no = futures[future]
                    self._print(f"  ❌ {permit_no} 未預期錯誤: {e}")

if __name__ == '__main__':
    try:
        sync = PermitSync()
        sync.run()
    except KeyboardInterrupt:
        print("\n🛑 使用者手動停止")
    except Exception as e:
        print(f"\n❌ 發生未預期錯誤: {e}")