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
SERVICE_ACCOUNT_FILE = '/Users/geothingsmacbookair/Downloads/credentials.json'
SCOPES = ['https://www.googleapis.com/auth/drive']
SHARED_DRIVE_ID = '0AIvp1h-6BZ1oUk9PVA'
PDF_LIST_URL = 'https://www-ws.gov.taipei/001/Upload/845/relfile/-1/845/2a1b5b50-13e7-4378-bcb2-03888bf5c462.pdf'
STATE_FILE = './state/sync_permits_progress.json'
# ============================================

# 初始化 Google Drive API
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=credentials)

class PermitSync:
    def __init__(self):
        self.target_folders = {}
        self.permit_mapping = {} 
        self.state = self.load_state()
        self.restricted_files = []
        
    def load_state(self) -> dict:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {'processed': [], 'skipped': [], 'errors': [], 'restricted': []}
    
    def save_state(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.state, indent=2, ensure_ascii=False, fp=f)
    
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
            results = drive_service.files().list(
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
    
    def check_file_exists(self, folder_id: str, filename: str, path: str = "") -> bool:
        target_folder_id = folder_id
        if path:
            target_folder_id = self.get_or_create_subfolder(folder_id, path)
            if not target_folder_id: return False
        try:
            query = f"'{target_folder_id}' in parents and (name='{filename}' or name='{filename}.url') and trashed=false"
            results = drive_service.files().list(
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
            try:
                query = f"'{current_folder_id}' in parents and name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
                results = drive_service.files().list(q=query, fields='files(id)', supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
                items = results.get('files', [])
                if items:
                    current_folder_id = items[0]['id']
                else:
                    file_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [current_folder_id]}
                    folder = drive_service.files().create(body=file_metadata, fields='id', supportsAllDrives=True).execute()
                    current_folder_id = folder['id']
            except HttpError:
                return None
        return current_folder_id

    def create_shortcut_file(self, parent_id: str, filename: str, web_link: str):
        try:
            link_filename = f"{filename}.url"
            file_content = f"[InternetShortcut]\nURL={web_link}"
            file_metadata = {'name': link_filename, 'parents': [parent_id], 'mimeType': 'text/plain'}
            media = MediaIoBaseUpload(io.BytesIO(file_content.encode('utf-8')), mimetype='text/plain', resumable=True)
            drive_service.files().create(body=file_metadata, media_body=media, fields='id', supportsAllDrives=True).execute()
            return True
        except Exception:
            return False

    def copy_file(self, source_file_id: str, target_folder_id: str, filename: str, path: str = ""):
        final_folder_id = target_folder_id
        if path:
            final_folder_id = self.get_or_create_subfolder(target_folder_id, path)
            if not final_folder_id: return None, None
        
        try:
            file_metadata = {'name': filename, 'parents': [final_folder_id]}
            copied_file = drive_service.files().copy(fileId=source_file_id, body=file_metadata, fields='id', supportsAllDrives=True).execute()
            return copied_file['id'], final_folder_id
        except HttpError as e:
            try:
                request = drive_service.files().get_media(fileId=source_file_id, supportsAllDrives=True)
                file_buffer = io.BytesIO()
                downloader = MediaIoBaseDownload(file_buffer, request)
                done = False
                while not done: status, done = downloader.next_chunk()
                file_buffer.seek(0)
                media = MediaIoBaseUpload(file_buffer, mimetype='application/pdf', resumable=True)
                uploaded_file = drive_service.files().create(
                    body={'name': filename, 'parents': [final_folder_id], 'mimeType': 'application/pdf'},
                    media_body=media, fields='id', supportsAllDrives=True).execute()
                return uploaded_file['id'], final_folder_id
            except HttpError as download_error:
                if 'cannotDownloadFile' in str(download_error) or 'cannotCopyFile' in str(e):
                    return 'restricted', final_folder_id
                return None, None

    def sync_permit(self, permit_no: str, source_url: str, target_folder_id: str):
        print(f"\n🔄 監測建案: {permit_no}")
        source_folder_id = self.extract_folder_id_from_url(source_url)
        if not source_folder_id:
            # 有連結但無法解析ID (例如不是資料夾連結)，記錄錯誤
            self.state['errors'].append({'permit': permit_no, 'error': 'Invalid URL ID'})
            return
        
        try:
            files = self.list_files_recursive(source_folder_id)
            if not files:
                print(f"  ⚠️ 來源無檔案")
                return
            
            copied = 0
            
            for file_id, filename, path, web_link in files:
                display_path = f"{path}/{filename}" if path else filename
                
                if self.check_file_exists(target_folder_id, filename, path):
                    continue
                
                result_id, final_folder_id = self.copy_file(file_id, target_folder_id, filename, path)
                if result_id == 'restricted':
                    print(f"    🔒 受限: {display_path} (建立捷徑)")
                    self.create_shortcut_file(final_folder_id, filename, web_link)
                    self.restricted_files.append({'filename': filename, 'permit': permit_no})
                elif result_id:
                    print(f"    🆕 發現新檔並複製: {display_path}")
                    copied += 1
                time.sleep(0.1)  # 優化：從 0.5 秒減少到 0.1 秒
            
            if copied > 0:
                print(f"  📊 更新完成: 新增 {copied} 個")

            if permit_no not in self.state['processed']:
                self.state['processed'].append(permit_no)
            self.save_state()
            
        except Exception as e:
            print(f"  ❌ 處理中斷: {e}")
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

        # 優化：過濾掉已處理且無錯誤的建案（增量同步）
        unprocessed_permits = []
        for permit_no, source_url in permit_list:
            # 如果建案已成功處理過，跳過
            if permit_no in self.state['processed']:
                # 檢查是否有錯誤記錄，如有則重新處理
                has_error = any(e.get('permit') == permit_no for e in self.state.get('errors', []))
                if not has_error:
                    continue
            unprocessed_permits.append((permit_no, source_url))

        print(f"\n📋 監測目標: {len(permit_list)} 個建案")
        print(f"✅ 已處理: {len(permit_list) - len(unprocessed_permits)} 個")
        print(f"🔄 待處理: {len(unprocessed_permits)} 個")

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

            self.sync_permit(permit_no, source_url, target_id)

if __name__ == '__main__':
    try:
        sync = PermitSync()
        sync.run()
    except KeyboardInterrupt:
        print("\n🛑 使用者手動停止")
    except Exception as e:
        print(f"\n❌ 發生未預期錯誤: {e}")