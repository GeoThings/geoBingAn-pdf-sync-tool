#!/usr/bin/env python3
"""
建照監測追蹤報告生成工具

功能：
1. 掃描 Google Drive 取得建照資料夾和 PDF 數量
2. 從 geoBingAn API 取得系統中的 PDF 資料
3. 解析台北市政府 PDF 識別非 Google Drive 雲端服務
4. 生成 HTML 互動報告和 CSV 匯出檔
"""
import csv
import json
import os
import re
import sys
import time
import requests
import urllib3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import pypdf
from jwt_auth import get_valid_token as _jwt_get_valid_token
from permit_utils import extract_name_from_filename
from report_template import generate_html_report, generate_csv_report

import warnings

# 匯入配置
try:
    from config import (
        JWT_TOKEN,
        USER_EMAIL,
        GROUP_ID,
        REFRESH_TOKEN,
        GEOBINGAN_REFRESH_URL
    )
    try:
        from config import SHARED_DRIVE_ID
    except ImportError:
        SHARED_DRIVE_ID = os.environ.get('SHARED_DRIVE_ID', '0AIvp1h-6BZ1oUk9PVA')
    print(f"✅ 已載入認證配置（用戶: {USER_EMAIL}）")
except ImportError as e:
    print("❌ 找不到 config.py 或缺少必要設定")
    sys.exit(1)

# ================== 設定區域 ==================
SERVICE_ACCOUNT_FILE = os.environ.get(
    'GOOGLE_CREDENTIALS',
    os.path.join(os.path.dirname(__file__), 'credentials.json')
)
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
PDF_LIST_URL_DEFAULT = 'https://www-ws.gov.taipei/001/Upload/845/relfile/-1/845/03b35db7-a123-4b29-b881-1cb17fa9c4f2.pdf'
PDF_LIST_URL = PDF_LIST_URL_DEFAULT

# API 設定
GEOBINGAN_API_BASE = 'https://riskmap.today/api/reports/construction-reports/'

# 輸出檔案
STATE_DIR = './state'
OUTPUT_HTML = f'{STATE_DIR}/permit_tracking_report.html'
OUTPUT_CSV = f'{STATE_DIR}/permit_tracking.csv'
NON_GOOGLE_JSON = f'{STATE_DIR}/non_google_permits.json'
MAPPING_JSON = f'{STATE_DIR}/permit_system_mapping.json'
ALERT_DATA_CSV = f'{STATE_DIR}/alert_data.csv'
# ============================================

# 全域變數
current_access_token = JWT_TOKEN


def get_valid_token() -> str:
    """取得有效的 Token（使用 jwt_auth 模組），刷新時同步寫回 .env"""
    global current_access_token
    valid_token, was_refreshed, new_refresh = _jwt_get_valid_token(
        current_access_token, REFRESH_TOKEN, GEOBINGAN_REFRESH_URL
    )
    if was_refreshed:
        current_access_token = valid_token
        try:
            from config import update_jwt_token
            update_jwt_token(valid_token, new_refresh)
        except Exception as e:
            print(f"⚠️  無法更新 .env Token: {e}")
    return current_access_token


def init_drive_service():
    """初始化 Google Drive API"""
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build('drive', 'v3', credentials=credentials)


def scan_google_drive(service) -> Dict[str, dict]:
    """掃描 Google Drive 取得所有建照資料夾"""
    print("\n📂 掃描 Google Drive 建照資料夾...")

    from drive_utils import list_top_level_folders, list_all_subfolders, build_folder_resolver

    raw_folders = list_top_level_folders(
        service, SHARED_DRIVE_ID,
        fields='nextPageToken, files(id, name, modifiedTime)'
    )

    from permit_utils import normalize_permit as _normalize
    permit_folders = {}
    for folder in raw_folders:
        permit_num = _normalize(folder['name'])
        if permit_num:
            permit_folders[permit_num] = {
                'folder_id': folder['id'],
                'folder_name': folder['name'],
                'modified_time': folder.get('modifiedTime', '')
            }

    print(f"  找到 {len(permit_folders)} 個建照資料夾")

    print("  掃描子資料夾結構...")
    folder_id_to_permit = {info['folder_id']: permit for permit, info in permit_folders.items()}
    all_folders = list_all_subfolders(service, SHARED_DRIVE_ID)
    resolve_permit = build_folder_resolver(folder_id_to_permit, all_folders)

    for fid in list(all_folders.keys()):
        resolved = resolve_permit(fid)
        if resolved:
            folder_id_to_permit[fid] = resolved
    subfolder_count = len(folder_id_to_permit) - len(permit_folders)
    print(f"    已建立 {len(permit_folders)} 個建案資料夾 + {subfolder_count} 個子資料夾的對應")

    # 用單一查詢掃描所有 PDF，再按資料夾分組統計（取代逐資料夾查詢）
    print("  掃描所有 PDF 並統計...")

    # 收集每個資料夾的 unique 檔名和最新修改時間
    folder_names = {}   # permit → set of unique filenames
    folder_latest = {}  # permit → latest modifiedTime

    page_token = None
    page_count = 0
    while True:
        try:
            results = service.files().list(
                q="mimeType='application/pdf' and trashed=false",
                corpora='drive',
                driveId=SHARED_DRIVE_ID,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields='nextPageToken, files(name, modifiedTime, parents)',
                pageSize=1000,
                pageToken=page_token
            ).execute()

            page_count += 1
            for f in results.get('files', []):
                parents = f.get('parents', [])
                if not parents:
                    continue
                permit = folder_id_to_permit.get(parents[0])
                if permit:
                    if permit not in folder_names:
                        folder_names[permit] = set()
                        folder_latest[permit] = ''
                    folder_names[permit].add(f.get('name', ''))
                    mod_time = f.get('modifiedTime', '')
                    if mod_time > folder_latest[permit]:
                        folder_latest[permit] = mod_time

            page_token = results.get('nextPageToken')
            if not page_token:
                break
        except HttpError as e:
            print(f"    ❌ PDF 掃描第 {page_count + 1} 頁失敗: {e}")
            print(f"    ❌ 丟棄部分結果，改為逐資料夾完整掃描...")
            # 批次掃描中斷，無法確定哪些資料夾完整掃到，
            # 全部丟棄改用逐資料夾查詢確保完整性
            folder_names.clear()
            folder_latest.clear()
            for permit, info in permit_folders.items():
                try:
                    fallback = service.files().list(
                        q=f"'{info['folder_id']}' in parents and mimeType='application/pdf'",
                        corpora='drive',
                        driveId=SHARED_DRIVE_ID,
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                        fields='files(name, modifiedTime)',
                        pageSize=1000
                    ).execute()
                    files = fallback.get('files', [])
                    folder_names[permit] = set(f.get('name', '') for f in files)
                    if files:
                        folder_latest[permit] = max(f.get('modifiedTime', '') for f in files)
                    else:
                        folder_latest[permit] = ''
                except HttpError:
                    folder_names[permit] = set()
                    folder_latest[permit] = ''
            break

    # 寫回 permit_folders
    total_unique = 0
    for permit in permit_folders:
        unique_count = len(folder_names.get(permit, set()))
        permit_folders[permit]['pdf_count'] = unique_count
        permit_folders[permit]['latest_pdf'] = folder_latest.get(permit, '')
        total_unique += unique_count

    print(f"    統計完成: {total_unique} 個唯一 PDF 分布在 {len(permit_folders)} 個資料夾")

    # 從 PDF 檔名提取建案名稱
    drive_names = {}  # permit → {name: count}
    for permit, filenames in folder_names.items():
        for filename in filenames:
            name = extract_name_from_filename(filename)
            if name:
                if permit not in drive_names:
                    drive_names[permit] = {}
                drive_names[permit][name] = drive_names[permit].get(name, 0) + 1

    # 選出每個建案最常出現的名稱
    permit_folders['_drive_names'] = {}
    for permit, counts in drive_names.items():
        best = max(counts.items(), key=lambda x: x[1])[0]
        permit_folders['_drive_names'][permit] = best

    print(f"    從 PDF 檔名提取 {len(drive_names)} 個建案名稱")

    return permit_folders


def load_filename_to_permit_mapping() -> Dict[str, str]:
    """從上傳記錄建立檔名到建照的對應"""
    mapping = {}
    permit_pattern = r'(\d{2,3}建字第\d{3,5}號)'

    # 優先使用永久歷史記錄
    history_file = './state/upload_history_all.json'
    state_file = './state/uploaded_to_geobingan_7days.json'

    all_files = []

    # 載入永久歷史記錄
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
            all_files.extend(history.get('uploaded_files', []))
        except Exception as e:
            print(f"  載入永久歷史記錄時發生錯誤: {e}")

    # 也載入 7 天記錄（補充）
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
            for f in state.get('uploaded_files', []):
                if f not in all_files:
                    all_files.append(f)
        except Exception:
            pass

    print(f"  載入 {len(all_files)} 個上傳記錄")

    for item in all_files:
        # 處理不同格式：可能是字串或 dict
        if isinstance(item, dict):
            filepath = item.get('permit', '') + '/' + item.get('pdf', {}).get('name', '')
        else:
            filepath = str(item)

        match = re.search(permit_pattern, filepath)
        if match:
            permit = match.group(1)
            # 取得檔名（可能在子資料夾中）
            filename = filepath.split('/')[-1] if '/' in filepath else filepath
            mapping[filename] = permit

            # 也處理加了 .pdf 的情況
            if not filename.lower().endswith('.pdf'):
                mapping[filename + '.pdf'] = permit

    return mapping


def load_upload_history_by_permit() -> Dict[str, set]:
    """從上傳記錄取得每個建案已上傳的檔案"""
    permit_files = {}
    history_file = './state/upload_history_all.json'

    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)

            for item in history.get('uploaded_files', []):
                if isinstance(item, str) and '/' in item:
                    parts = item.split('/', 1)
                    permit = parts[0]
                    filename = parts[1]
                    if permit not in permit_files:
                        permit_files[permit] = set()
                    permit_files[permit].add(filename)
        except Exception as e:
            print(f"  載入上傳記錄時發生錯誤: {e}")

    return permit_files



def fetch_api_reports() -> Dict[str, List[dict]]:
    """從 geoBingAn API 取得所有報告，並結合上傳記錄"""
    print("\n📡 從 geoBingAn API 取得報告資料...")

    # 載入上傳記錄（按建案分組）
    upload_history = load_upload_history_by_permit()
    total_uploaded = sum(len(files) for files in upload_history.values())
    print(f"  載入 {total_uploaded} 個上傳記錄")

    # 先載入檔名對應（用於 API 報告匹配）
    filename_to_permit = load_filename_to_permit_mapping()
    print(f"  已載入 {len(filename_to_permit)} 個檔名對應")

    token = get_valid_token()
    headers = {'Authorization': f'Bearer {token}'}

    all_reports = []
    page = 1
    auth_retries = 0

    while True:
        try:
            response = requests.get(
                f"{GEOBINGAN_API_BASE}?group_id={GROUP_ID}&page={page}&page_size=100",
                headers=headers,
                timeout=30
            )

            if response.status_code == 401:
                auth_retries += 1
                if auth_retries > 2:
                    print("  ❌ 401 重試超過上限，停止")
                    break
                token = get_valid_token()
                if token:
                    headers = {'Authorization': f'Bearer {token}'}
                    continue
                break
            auth_retries = 0

            if response.status_code != 200:
                print(f"  API 錯誤: {response.status_code}")
                break

            data = response.json()
            results = data.get('results', [])

            if not results:
                break

            all_reports.extend(results)
            print(f"    第 {page} 頁: {len(results)} 筆")

            if not data.get('next'):
                break
            page += 1

        except Exception as e:
            print(f"  API 請求錯誤: {e}")
            break

    print(f"  共取得 {len(all_reports)} 筆報告")

    # 高信度匹配：api_match 名稱 → permit（已通過 construction-projects API 驗證）
    registry_file = './state/permit_registry.json'
    api_name_to_permits = {}  # api_match name → [permits]（支援多對一）
    if os.path.exists(registry_file):
        with open(registry_file, 'r', encoding='utf-8') as f:
            _reg_tmp = json.load(f)
        for permit, info in _reg_tmp.items():
            api_match = info.get('api_match', '')
            if api_match and len(api_match) >= 4:
                if api_match not in api_name_to_permits:
                    api_name_to_permits[api_match] = []
                api_name_to_permits[api_match].append(permit)
        print(f"  高信度 API 名稱: {len(api_name_to_permits)} 個")

    # 從 permit_registry.json + construction-projects API 建立名稱對應
    name_to_permit_fuzzy = {}  # 完整名稱 → permit
    fragment_to_permit = {}    # 名稱片段（≥4字） → permit（唯一對應才使用）
    if os.path.exists(registry_file):
        with open(registry_file, 'r', encoding='utf-8') as f:
            registry = json.load(f)
        # 收集所有名稱片段（用於去重）
        fragment_permits = {}  # fragment → set of permits
        # 通用名稱和太短的名稱不可用於模糊匹配（會造成大量誤配）
        generic_patterns = re.compile(r'^(監測報告?|監測報表?|監測$|安全觀測|安全監測|觀測報告|觀測數據|工地監測|工地$|報告$|報表$|告示牌|基地觀測系統|建號\d)')
        for permit, info in registry.items():
            name = info.get('name', '')
            if name and len(name) >= 4 and not generic_patterns.match(name):
                name_to_permit_fuzzy[name] = permit
                # 提取 3~6 字的滑動視窗片段（≥3字避免公司名/地名誤匹配）
                clean = re.sub(r'(安全觀測|監測報表?|觀測報告|觀測數據|新建工程|工程|報告|報表|數據)', '', name)
                # 只保留中文字元片段
                cjk_parts = re.findall(r'[\u4e00-\u9fff]+', clean)
                for part in cjk_parts:
                    for flen in range(4, min(7, len(part) + 1)):
                        for start in range(len(part) - flen + 1):
                            frag = part[start:start + flen]
                            if frag not in fragment_permits:
                                fragment_permits[frag] = set()
                            fragment_permits[frag].add(permit)
            # 也用 api_match 的名稱
            api_name = info.get('api_match', '')
            if api_name:
                name_to_permit_fuzzy[api_name] = permit
        # 只保留唯一對應的片段（避免歧義）
        for frag, permits in fragment_permits.items():
            if len(permits) == 1:
                fragment_to_permit[frag] = next(iter(permits))

    # 按建照號碼分組（結合 API 報告和上傳記錄）
    permit_reports = {}
    matched = 0
    unmatched = 0
    fuzzy_matched = 0

    # 先從上傳記錄建立基礎計數
    for permit, files in upload_history.items():
        permit_reports[permit] = [{'filename': f, 'created_at': '', 'status': 'uploaded'} for f in files]

    # 再處理 API 報告（避免重複計算）
    for report in all_reports:
        filename = report.get('file_name', '') or report.get('original_filename', '')

        # 方法 1: 直接從檔名找建照號
        permit_match = re.search(r'(\d{2,3}建字第\d{3,5}號)', filename)
        permit = None

        if permit_match:
            permit = permit_match.group(1)
        else:
            # 方法 2: 從上傳記錄對應
            permit = filename_to_permit.get(filename)

        # 方法 2.5: 高信度 API 名稱匹配（api_match 已驗證的名稱）
        if not permit and api_name_to_permits:
            fn_clean_api = re.sub(r'\d{7,8}|\d{4}[-/.]\d{2}[-/.]\d{2}|\.pdf$|[^\u4e00-\u9fff\w\s-]', '', filename)
            best_permits = None
            best_api_len = 0
            for api_name, permits_list in api_name_to_permits.items():
                # 從 API 名稱逐步去除通用後綴，提取核心名稱
                api_core = api_name
                for _ in range(3):
                    api_core = re.sub(r'[-\s]*(新建工程|監測案|新建統包工程|集合住宅|住宅大樓|商辦大樓|店鋪|工程|監測)$', '', api_core).strip()
                if len(api_core) >= 3 and api_core in fn_clean_api and len(api_core) > best_api_len:
                    best_permits = permits_list
                    best_api_len = len(api_core)
            if best_permits:
                # 將報告分配給所有對應的 permit
                for p in best_permits:
                    if p not in permit_reports:
                        permit_reports[p] = []
                    existing = {r['filename'] for r in permit_reports[p]}
                    if filename not in existing:
                        permit_reports[p].append({
                            'filename': filename,
                            'created_at': report.get('created_at', ''),
                            'status': report.get('parse_status', report.get('status', ''))
                        })
                matched += 1
                fuzzy_matched += 1
                continue  # 跳過後續匹配

        # 方法 3: 用 permit_registry 名稱模糊匹配
        if not permit and name_to_permit_fuzzy:
            best_match = None
            best_len = 0
            # 3a: 完整名稱在檔名中
            for name, p in name_to_permit_fuzzy.items():
                if name in filename and len(name) > best_len:
                    best_match = p
                    best_len = len(name)
            # 3b: 名稱片段匹配（最長唯一片段優先）
            if not best_match and fragment_to_permit:
                # 從檔名中去除日期、常見字詞和標點後嘗試匹配
                fn_clean = re.sub(r'\d{7,8}|\d{4}[-/.]\d{2}[-/.]\d{2}|\.pdf$|監測報告|觀測報告|報告|報表|[^\u4e00-\u9fff]', '', filename)
                for frag, p in sorted(fragment_to_permit.items(), key=lambda x: -len(x[0])):
                    if frag in fn_clean:
                        best_match = p
                        best_len = len(frag)
                        break
            # 3c: 反向匹配 — 從檔名提取 4~6 字子片段，在 registry 名稱中搜尋（唯一匹配才採用）
            if not best_match and name_to_permit_fuzzy:
                fn_clean = re.sub(r'\d{7,8}|\d{4}[-/.]\d{2}[-/.]\d{2}|\.pdf$|監測報告|觀測報告|觀測數據|報告|報表|新建工程|集合住宅|住宅大樓|安全觀測|安全監測|[^\u4e00-\u9fff]', '', filename)
                cjk_text = ''.join(re.findall(r'[\u4e00-\u9fff]+', fn_clean))
                # 從長到短嘗試，最短 4 字（避免短片段誤配）
                for flen in range(min(6, len(cjk_text)), 3, -1):
                    found = False
                    for start in range(len(cjk_text) - flen + 1):
                        sub = cjk_text[start:start + flen]
                        matches = [p for name, p in name_to_permit_fuzzy.items() if sub in name]
                        unique_permits = set(matches)
                        if len(unique_permits) == 1:
                            best_match = next(iter(unique_permits))
                            fuzzy_matched += 1
                            found = True
                            break
                    if found:
                        break
            if best_match:
                permit = best_match
                fuzzy_matched += 1

        if permit:
            matched += 1
            # 只有當檔案不在上傳記錄中時才加入（避免重複）
            if permit not in permit_reports:
                permit_reports[permit] = []

            # 檢查是否已存在
            existing_files = {r['filename'] for r in permit_reports[permit]}
            if filename not in existing_files:
                permit_reports[permit].append({
                    'filename': filename,
                    'created_at': report.get('created_at', ''),
                    'status': report.get('parse_status', report.get('status', ''))
                })
        else:
            unmatched += 1

    print(f"  對應成功: {matched}（含名稱匹配 {fuzzy_matched}）, 未對應: {unmatched}")
    return permit_reports


def download_and_parse_gov_pdf() -> List[dict]:
    """下載並解析台北市政府 PDF，識別非 Google Drive 雲端服務"""
    print("\n📥 下載台北市政府建案列表...")

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', category=urllib3.exceptions.InsecureRequestWarning)
            response = requests.get(PDF_LIST_URL, verify=False, timeout=30)
        pdf_path = '/tmp/permit_list.pdf'
        with open(pdf_path, 'wb') as f:
            f.write(response.content)
        print(f"  已下載: {len(response.content)} bytes")
    except Exception as e:
        print(f"  下載失敗: {e}")
        return []

    print("  解析 PDF 內容...")
    non_google_permits = []

    try:
        with open(pdf_path, 'rb') as f:
            pdf_reader = pypdf.PdfReader(f)
            all_text = '\n'.join(p.extract_text() or '' for p in pdf_reader.pages)

        # 找出所有建照號碼位置
        permit_pattern = r'(\d{2,3}建字第\d{3,5}號)'
        permit_matches = list(re.finditer(permit_pattern, all_text))

        # 定義非 Google Drive 的雲端服務
        cloud_patterns = {
            'SharePoint': r'(sharepoint\.com[^\s]*)',
            'Dropbox': r'(dropbox\.com[^\s]*)',
            'OneDrive': r'(onedrive\.live\.com[^\s]*|1drv\.ms[^\s]*)',
            'MEGA': r'(mega\.nz[^\s]*)',
            'pCloud': r'(pcloud\.com[^\s]*)',
            'GoFile': r'(gofile\.io[^\s]*)',
            'ownCloud': r'(owncloud[^\s]*)',
            '短網址': r'(reurl\.cc[^\s]*|bit\.ly[^\s]*|tinyurl\.com[^\s]*)',
        }

        for i, match in enumerate(permit_matches):
            permit = match.group(1)
            start = match.end()
            end = permit_matches[i + 1].start() if i + 1 < len(permit_matches) else len(all_text)
            chunk = all_text[start:end]

            # 檢查是否有 Google Drive 連結
            has_google = bool(re.search(r'drive\.google\.com|docs\.google\.com', chunk))

            if not has_google:
                # 找出使用的雲端服務
                cloud_service = None
                cloud_url = None

                for service, pattern in cloud_patterns.items():
                    url_match = re.search(pattern, chunk, re.IGNORECASE)
                    if url_match:
                        cloud_service = service
                        cloud_url = url_match.group(1)
                        break

                # 檢查其他 http/https 連結
                if not cloud_service:
                    other_url = re.search(r'(https?://[^\s\)]+)', chunk)
                    if other_url:
                        url = other_url.group(1)
                        if 'gov.taipei' not in url and 'riskmap' not in url:
                            domain = re.search(r'https?://([^/]+)', url)
                            if domain:
                                cloud_service = f"其他: {domain.group(1)}"
                                cloud_url = url

                if cloud_service:
                    non_google_permits.append({
                        'permit': permit,
                        'cloud': cloud_service,
                        'url': cloud_url or ''
                    })

        print(f"  找到 {len(non_google_permits)} 個使用非 Google Drive 的建照")

    except Exception as e:
        print(f"  解析錯誤: {e}")

    return non_google_permits


def load_alert_data() -> Tuple[Dict[str, dict], Dict[str, str]]:
    """載入警戒/行動值資料，並對應到建照號碼。同時返回建案名稱對應表"""
    print("\n📊 載入警戒值資料...")

    # 建立建案名稱到建照號碼的對應（雙向）
    name_to_permit = {}
    permit_to_name = {}  # 建照號碼 -> 建案名稱
    permit_name_counts = {}  # 建照號碼 -> {名稱: 出現次數}
    history_file = './state/upload_history_all.json'

    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history = json.load(f)

            for item in history.get('uploaded_files', []):
                if isinstance(item, str) and '/' in item:
                    parts = item.split('/', 1)
                    permit = parts[0]
                    filename = parts[1]

                    # 只接受合法的建照號碼格式
                    if not re.match(r'\d{2,3}建字第\d{3,5}號', permit):
                        continue

                    name = extract_name_from_filename(filename)
                    if name and permit:
                        name_to_permit[name] = permit
                        # 統計每個名稱出現次數
                        if permit not in permit_name_counts:
                            permit_name_counts[permit] = {}
                        permit_name_counts[permit][name] = permit_name_counts[permit].get(name, 0) + 1

            # 選擇最常出現的名稱
            for permit, name_counts in permit_name_counts.items():
                if name_counts:
                    # 找出現次數最多的名稱
                    most_common = max(name_counts.items(), key=lambda x: x[1])
                    permit_to_name[permit] = most_common[0]

        except Exception as e:
            print(f"  載入上傳記錄時發生錯誤: {e}")

    if not os.path.exists(ALERT_DATA_CSV):
        print(f"  找不到警戒資料檔案: {ALERT_DATA_CSV}")
        return {}, permit_to_name

    # 讀取警戒資料 CSV
    alert_data = {}
    try:
        with open(ALERT_DATA_CSV, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                building_name = row.get('建案名稱', '').strip()
                warning_count = int(row.get('警戒次數', 0) or 0)
                action_count = int(row.get('行動次數', 0) or 0)
                alert_count = int(row.get('alert次數', 0) or 0)
                report_count = int(row.get('報告次數', 0) or 0)

                # 從範例檔名嘗試提取建照號碼
                example_files = row.get('原始檔名範例', '')
                permit = None

                # 方法1: 從範例檔名找建照號碼
                permit_match = re.search(r'(\d{2,3}建字第\d{3,5}號)', example_files)
                if permit_match:
                    permit = permit_match.group(1)

                # 方法2: 用建案名稱對應
                if not permit:
                    permit = name_to_permit.get(building_name)

                # 方法3: 嘗試部分匹配
                if not permit:
                    for name, p in name_to_permit.items():
                        if building_name in name or name in building_name:
                            permit = p
                            break

                if permit:
                    # 取得最近日期
                    latest_alert_date = row.get('最近日期', '')

                    # 如果同一建照有多筆資料，累加並保留最新日期
                    if permit in alert_data:
                        alert_data[permit]['warning_count'] += warning_count
                        alert_data[permit]['action_count'] += action_count
                        alert_data[permit]['alert_count'] += alert_count
                        alert_data[permit]['report_count'] += report_count
                        # 保留較新的日期
                        if latest_alert_date > alert_data[permit].get('latest_alert_date', ''):
                            alert_data[permit]['latest_alert_date'] = latest_alert_date
                    else:
                        alert_data[permit] = {
                            'warning_count': warning_count,
                            'action_count': action_count,
                            'alert_count': alert_count,
                            'report_count': report_count,
                            'latest_alert_date': latest_alert_date
                        }
                    # 更新建案名稱（用 extract 清理後再存入）
                    clean_name = extract_name_from_filename(building_name)
                    if clean_name:
                        permit_to_name[permit] = clean_name

        print(f"  已載入 {len(alert_data)} 個建照的警戒資料")
        print(f"  已載入 {len(permit_to_name)} 個建案名稱")
    except Exception as e:
        print(f"  載入警戒資料時發生錯誤: {e}")

    return alert_data, permit_to_name



def main(city: dict = None):
    """主程式"""
    if city:
        global SHARED_DRIVE_ID, PDF_LIST_URL
        SHARED_DRIVE_ID = city.get('shared_drive_id') or SHARED_DRIVE_ID
        PDF_LIST_URL = city.get('pdf_list_url') or PDF_LIST_URL
    city_name = city.get('name', '') if city else ''
    print("=" * 50)
    print(f"建照監測追蹤報告生成工具{f' ({city_name})' if city_name else ''}")
    print("=" * 50)

    start_time = time.time()

    # 初始化
    drive_service = init_drive_service()

    # 1. 掃描 Google Drive
    drive_data = scan_google_drive(drive_service)

    # 2. 從 API 取得報告
    api_reports = fetch_api_reports()

    # 3. 解析政府 PDF 取得非 Google 建照
    non_google = download_and_parse_gov_pdf()

    # 4. 合併資料
    print("\n🔄 合併資料...")
    permit_data = {}
    non_google_set = {item['permit'] for item in non_google}
    now = datetime.now()

    # 處理 Google Drive 資料
    for permit, info in drive_data.items():
        system_reports = api_reports.get(permit, [])
        system_count = len(system_reports)
        drive_count = info.get('pdf_count', 0)

        # 計算最新報告日期（取 Drive 時間和 API 報告時間中較新的）
        latest_report = info.get('latest_pdf', '')
        for sr in system_reports:
            api_date = sr.get('created_at', '')
            if api_date and api_date > latest_report:
                latest_report = api_date

        # 計算天數
        days_since = ''
        if latest_report:
            try:
                latest_dt = datetime.fromisoformat(latest_report.replace('Z', '+00:00'))
                days_since = (now - latest_dt.replace(tzinfo=None)).days
            except:
                pass

        # 判斷狀態
        if drive_count == 0:
            status = 'no_reports'
        elif system_count >= drive_count:
            status = 'completed'
        elif system_count > 0:
            status = 'in_progress'
        else:
            status = 'not_uploaded'

        permit_data[permit] = {
            'drive_count': drive_count,
            'system_count': system_count,
            'status': status,
            'latest_report': latest_report,
            'days_since_update': days_since,
            'folder_id': info.get('folder_id', '')
        }

    # 加入非 Google 建照
    for item in non_google:
        permit = item['permit']
        if permit not in permit_data:
            permit_data[permit] = {
                'drive_count': 0,
                'system_count': 0,
                'status': 'no_reports',
                'latest_report': '',
                'days_since_update': '',
                'folder_id': '',
                'cloud_service': item['cloud']
            }

    # 5. 從 permit_registry.json 載入建案名稱和即時警戒值（由 match_permits.py 產生）
    registry_file = './state/permit_registry.json'
    permit_names = {}
    alert_data = {}
    if os.path.exists(registry_file):
        with open(registry_file, 'r', encoding='utf-8') as f:
            registry = json.load(f)
        for permit, info in registry.items():
            reg_name = info.get('name', '')
            if reg_name:
                permit_names[permit] = reg_name
            # 從 live_alerts 載入即時警戒值
            la = info.get('live_alerts', {})
            if la and la.get('total', 0) > 0:
                alert_data[permit] = {
                    'warning_count': la.get('warning', 0),
                    'danger_count': la.get('danger', 0),
                    'total': la.get('total', 0),
                    'latest_alert_date': la.get('latest_date', ''),
                    'details': la.get('details', []),
                }
        print(f"  從 permit_registry 載入 {len(permit_names)} 個建案名稱，{len(alert_data)} 個有即時警戒值")
    else:
        print("  ⚠️ permit_registry.json 不存在，請先執行 python3 match_permits.py")
        registry = {}

    # 5b. 補充：從 upload_history 提取名稱（permit_registry 沒涵蓋的）
    _, history_names = load_alert_data()
    for permit, name in history_names.items():
        if name and permit not in permit_names:
            permit_names[permit] = name

    # 5c. 移除 _drive_names（掃描時產生的暫存資料）
    drive_data.pop('_drive_names', None)

    # 5e. 已完工建案標記：建照年份在 110 年（2021）前且無系統報告的視為已結案
    completed_count = 0
    for permit, data in permit_data.items():
        year_match = re.search(r'^(\d{2,3})建字第', permit)
        if year_match:
            roc_year = int(year_match.group(1))
            # 110 年前（含）的建照且沒有系統報告 → 很可能已完工
            if roc_year <= 110 and data.get('system_count', 0) == 0:
                if data.get('status') in ('not_uploaded', 'no_reports'):
                    data['status'] = 'completed_project'
                    completed_count += 1
    if completed_count > 0:
        print(f"  標記 {completed_count} 個建案為已結案（建照年份 ≤ 110 年且無系統報告）")

    # 6. 儲存資料
    with open(MAPPING_JSON, 'w', encoding='utf-8') as f:
        json.dump(permit_data, f, indent=2, ensure_ascii=False)

    with open(NON_GOOGLE_JSON, 'w', encoding='utf-8') as f:
        json.dump(non_google, f, indent=2, ensure_ascii=False)

    # 7. 生成報告
    generate_html_report(permit_data, non_google, alert_data, permit_names, output_path=OUTPUT_HTML)
    generate_csv_report(permit_data, non_google, alert_data, permit_names, output_path=OUTPUT_CSV)

    elapsed = time.time() - start_time
    print(f"\n✅ 報告生成完成！耗時 {elapsed:.1f} 秒")
    print(f"   - HTML: {OUTPUT_HTML}")
    print(f"   - CSV: {OUTPUT_CSV}")


if __name__ == '__main__':
    import argparse
    from city_config import get_cities_for_cli

    parser = argparse.ArgumentParser()
    parser.add_argument('--city', default=None, help='City ID or "all"')
    args = parser.parse_args()

    cities = get_cities_for_cli(args.city)
    for city in cities:
        main(city=city)
