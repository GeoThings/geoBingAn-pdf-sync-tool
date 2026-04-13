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
import PyPDF2
from jwt_auth import get_valid_token as _jwt_get_valid_token

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
PDF_LIST_URL = 'https://www-ws.gov.taipei/001/Upload/845/relfile/-1/845/03b35db7-a123-4b29-b881-1cb17fa9c4f2.pdf'

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
    """取得有效的 Token（使用 jwt_auth 模組）"""
    global current_access_token
    valid_token, was_refreshed = _jwt_get_valid_token(
        current_access_token, REFRESH_TOKEN, GEOBINGAN_REFRESH_URL
    )
    if was_refreshed:
        current_access_token = valid_token
    return current_access_token


def init_drive_service():
    """初始化 Google Drive API"""
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build('drive', 'v3', credentials=credentials)


def scan_google_drive(service) -> Dict[str, dict]:
    """掃描 Google Drive 取得所有建照資料夾"""
    print("\n📂 掃描 Google Drive 建照資料夾...")

    permit_folders = {}
    page_token = None

    while True:
        results = service.files().list(
            q=f"'{SHARED_DRIVE_ID}' in parents and mimeType='application/vnd.google-apps.folder'",
            corpora='drive',
            driveId=SHARED_DRIVE_ID,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields='nextPageToken, files(id, name, modifiedTime)',
            pageSize=1000,
            pageToken=page_token
        ).execute()

        for folder in results.get('files', []):
            permit_match = re.search(r'(\d{2,3}建字第\d{3,5}號)', folder['name'])
            if permit_match:
                permit_num = permit_match.group(1)
                permit_folders[permit_num] = {
                    'folder_id': folder['id'],
                    'folder_name': folder['name'],
                    'modified_time': folder.get('modifiedTime', '')
                }

        page_token = results.get('nextPageToken')
        if not page_token:
            break

    print(f"  找到 {len(permit_folders)} 個建照資料夾")

    # 用單一查詢掃描所有 PDF，再按資料夾分組統計（取代逐資料夾查詢）
    print("  掃描所有 PDF 並統計...")
    folder_id_to_permit = {info['folder_id']: permit for permit, info in permit_folders.items()}

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

    while True:
        try:
            response = requests.get(
                f"{GEOBINGAN_API_BASE}?group_id={GROUP_ID}&page={page}&page_size=100",
                headers=headers,
                timeout=30
            )

            if response.status_code == 401:
                token = refresh_access_token()
                if token:
                    headers = {'Authorization': f'Bearer {token}'}
                    continue
                break

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

    # 按建照號碼分組（結合 API 報告和上傳記錄）
    permit_reports = {}
    matched = 0
    unmatched = 0

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

    print(f"  對應成功: {matched}, 未對應: {unmatched}")
    return permit_reports


def download_and_parse_gov_pdf() -> List[dict]:
    """下載並解析台北市政府 PDF，識別非 Google Drive 雲端服務"""
    print("\n📥 下載台北市政府建案列表...")

    try:
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
            pdf_reader = PyPDF2.PdfReader(f)
            all_text = ""
            for page in pdf_reader.pages:
                text = page.extract_text()
                if text:
                    all_text += text + "\n"

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

                    # 從檔名提取建案名稱（去除日期和副檔名）
                    name = re.sub(r'^\d{7}', '', filename)  # 去除開頭日期
                    name = re.sub(r'\.pdf$', '', name, flags=re.IGNORECASE)  # 去除 .pdf
                    name = re.sub(r'_\d+$', '', name)  # 去除結尾數字
                    name = re.sub(r'報告$', '', name)  # 去除「報告」
                    name = re.sub(r'-\d{4}-\d{2}-\d{2}.*$', '', name)  # 去除日期格式
                    name = re.sub(r' NO\.\d+$', '', name)  # 去除 NO.xx
                    name = re.sub(r'-\d+$', '', name)  # 去除結尾數字
                    name = name.strip(' -_')

                    # 過濾通用名稱
                    generic_names = {'觀測紀錄', '監測數據', '安全觀測系統', '初始值', '整體進度',
                                     '觀測儀器配置圖', '量測報表', '報表', '日報告', '周報告', '月報告',
                                     '安全觀測', '監測報告', '工地監測報告', '專案區間報告書'}
                    if name and permit and len(name) >= 3 and name not in generic_names:
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
                    # 更新建案名稱（優先使用 CSV 中的名稱）
                    if building_name:
                        permit_to_name[permit] = building_name

        print(f"  已載入 {len(alert_data)} 個建照的警戒資料")
        print(f"  已載入 {len(permit_to_name)} 個建案名稱")
    except Exception as e:
        print(f"  載入警戒資料時發生錯誤: {e}")

    return alert_data, permit_to_name


def generate_html_report(permit_data: Dict[str, dict], non_google: List[dict], alert_data: Dict[str, dict] = None, permit_names: Dict[str, str] = None):
    """生成 HTML 報告"""
    import html as html_mod
    print("\n📊 生成 HTML 報告...")

    if alert_data is None:
        alert_data = {}
    if permit_names is None:
        permit_names = {}

    def esc(s: str) -> str:
        """Escape string for safe HTML insertion (text and attributes)"""
        return html_mod.escape(str(s), quote=True) if s else ''

    now = datetime.now()

    # 統計
    total = len(permit_data)
    completed = sum(1 for p in permit_data.values() if p.get('status') == 'completed')
    in_progress = sum(1 for p in permit_data.values() if p.get('status') == 'in_progress')
    not_uploaded = sum(1 for p in permit_data.values() if p.get('status') == 'not_uploaded')
    no_reports = sum(1 for p in permit_data.values() if p.get('status') == 'no_reports')
    other_cloud = len(non_google)
    errors = sum(1 for p in permit_data.values() if p.get('status') == 'error')

    # 建立非 Google 雲端服務分類
    cloud_groups = {}
    for item in non_google:
        cloud = item['cloud']
        if cloud not in cloud_groups:
            cloud_groups[cloud] = []
        cloud_groups[cloud].append(item['permit'])

    # 排序 (按數量)
    cloud_groups = dict(sorted(cloud_groups.items(), key=lambda x: -len(x[1])))

    # 雲端服務圖示
    cloud_icons = {
        'SharePoint': '📊', 'Dropbox': '📦', 'OneDrive': '☁️',
        'MEGA': '🔷', 'pCloud': '🌩️', 'GoFile': '📁',
        'ownCloud': '🔵', '短網址': '🔗'
    }

    # 生成雲端服務卡片
    cloud_cards_html = ""
    for cloud, permits in cloud_groups.items():
        icon = cloud_icons.get(cloud, '🌐')
        permits_html = ''.join([f'<li>{esc(p)}</li>' for p in permits[:20]])
        if len(permits) > 20:
            permits_html += f'<li>...還有 {len(permits) - 20} 個</li>'
        cloud_cards_html += f'''
<div class="cloud-card">
<h4><span class="icon">{icon}</span> {esc(cloud)} ({len(permits)})</h4>
<ul>{permits_html}</ul>
</div>'''

    # 建立非 Google 查詢表
    non_google_set = {item['permit']: item['cloud'] for item in non_google}

    # 需要關注：有警戒值的建案
    alert_permits = []
    for permit_key, pdata in permit_data.items():
        pa = alert_data.get(permit_key, {})
        wc = pa.get('warning_count', 0)
        ac = pa.get('action_count', 0)
        alc = pa.get('alert_count', 0)
        if wc > 0 or ac > 0 or alc > 0:
            parts = []
            if wc > 0: parts.append(f'⚠️{wc}')
            if ac > 0: parts.append(f'🚨{ac}')
            if alc > 0: parts.append(f'🔴{alc}')
            lad = pa.get('latest_alert_date', '')
            alert_permits.append({
                'permit': permit_key,
                'name': permit_names.get(permit_key, ''),
                'summary': ' '.join(parts),
                'latest_alert_date': lad[:10] if lad else '-',
            })

    # 需要關注：報告過期的建案 (days_since_update > 30 and status != 'no_reports')
    stale_permits = []
    for permit_key, pdata in permit_data.items():
        ds = pdata.get('days_since_update', '')
        st = pdata.get('status', '')
        if ds != '' and ds is not None and int(ds) > 30 and st != 'no_reports':
            stale_permits.append({
                'permit': permit_key,
                'name': permit_names.get(permit_key, ''),
                'days': int(ds),
                'latest': pdata.get('latest_report', '')[:10] if pdata.get('latest_report') else '-',
            })

    # HTML for 需要關注 cards
    attention_alert_cards = ""
    for ap in alert_permits:
        attention_alert_cards += f'<div class="attention-card attention-card-alert"><div class="ac-permit">{esc(ap["permit"])}</div><div class="ac-name">{esc(ap["name"] or "-")}</div><div class="ac-summary">{esc(ap["summary"])}</div><div class="ac-date">最近警戒: {esc(ap["latest_alert_date"])}</div></div>'

    attention_stale_rows = ""
    for sp in stale_permits:
        attention_stale_rows += f'<tr><td><strong>{esc(sp["permit"])}</strong></td><td>{esc(sp["name"] or "-")}</td><td><span class="days days-old" data-date="{esc(sp["latest"])}"></span></td></tr>'

    has_attention = len(alert_permits) > 0 or len(stale_permits) > 0
    attention_open = 'open' if has_attention else ''

    # 排序建照 (按號碼)
    sorted_permits = sorted(permit_data.keys(), key=lambda x: (
        int(re.search(r'(\d{2,3})建字', x).group(1)) if re.search(r'(\d{2,3})建字', x) else 0,
        int(re.search(r'第(\d+)號', x).group(1)) if re.search(r'第(\d+)號', x) else 0
    ), reverse=True)

    # 生成表格行
    rows_html = ""
    for i, permit in enumerate(sorted_permits, 1):
        data = permit_data[permit]
        cloud = non_google_set.get(permit, 'Google Drive')
        drive_count = data.get('drive_count', 0)
        system_count = data.get('system_count', 0)
        status = data.get('status', 'unknown')
        latest = data.get('latest_report', '')
        days = data.get('days_since_update', '')
        folder_id = data.get('folder_id', '')

        # 狀態 badge
        status_badges = {
            'completed': ('✔ 已完成', 'badge-success'),
            'in_progress': ('⏳ 分析中', 'badge-info'),
            'not_uploaded': ('⬆ 待上傳', 'badge-warning'),
            'no_reports': ('── 無資料', 'badge-gray'),
            'error': ('✖ 異常', 'badge-danger')
        }
        badge_text, badge_class = status_badges.get(status, ('未知', 'badge-gray'))

        # 雲端 badge
        if cloud == 'Google Drive':
            cloud_badge = ''
        else:
            cloud_badge = f'<span class="badge badge-orange">{esc(cloud)}</span>'

        # 覆蓋率
        if drive_count > 0 and system_count > 0:
            coverage = min(100, int(system_count / drive_count * 100))
            bar_color = '#22c55e' if coverage >= 80 else '#f59e0b' if coverage >= 50 else '#dc2626'
            coverage_html = f'<div class="progress-wrapper"><div class="progress-text">{coverage}%</div><div class="bar"><div class="bar-fill" style="width:{coverage}%;background:{bar_color}"></div></div></div>'
        else:
            coverage_html = '<span class="empty-val">-</span>'

        # 天數 - use data-date for dynamic JS calculation
        if latest:
            days_html = f'<span class="days" data-date="{latest[:10]}"></span>'
        else:
            days_html = '<span class="empty-val">-</span>'

        # 連結
        if folder_id:
            drive_link = f'<a href="https://drive.google.com/drive/folders/{folder_id}" target="_blank" title="開啟 Google Drive 資料夾">{drive_count} ↗</a>'
        else:
            drive_link = str(drive_count)

        # 最新報告日期
        latest_html = latest[:10] if latest else '<span class="empty-val">-</span>'

        # 建案名稱
        building_name = permit_names.get(permit, '')
        # 截斷過長的名稱
        if len(building_name) > 25:
            name_html = f'<span title="{esc(building_name)}">{esc(building_name[:25])}...</span>'
        else:
            name_html = esc(building_name) if building_name else '<span class="empty-val">-</span>'

        # 警戒/行動值
        permit_alert = alert_data.get(permit, {})
        warning_count = permit_alert.get('warning_count', 0)
        action_count = permit_alert.get('action_count', 0)
        alert_count = permit_alert.get('alert_count', 0)
        latest_alert_date = permit_alert.get('latest_alert_date', '')

        # 警戒值合併顯示
        alert_total = warning_count + action_count + alert_count
        alert_parts = []
        if warning_count > 0:
            alert_parts.append(f'⚠️{warning_count}')
        if action_count > 0:
            alert_parts.append(f'🚨{action_count}')
        if alert_count > 0:
            alert_parts.append(f'🔴{alert_count}')

        if alert_parts:
            alert_date_str = latest_alert_date[:10] if latest_alert_date else ''
            tooltip = f'最近警戒: {alert_date_str}' if alert_date_str else ''
            merged_alert_html = f'<span class="alert-merged" title="{tooltip}">{" ".join(alert_parts)}</span>'
        else:
            merged_alert_html = '<span class="empty-val">-</span>'

        # row CSS classes
        row_classes = []
        if alert_total > 0:
            row_classes.append('row-alert')
        # stale check done in JS via data-latest-date; add Python-side too for no_reports exclusion
        latest_date_attr = latest[:10] if latest else ''

        row_class_str = ' '.join(row_classes)

        rows_html += f'''
<tr data-status="{esc(status)}" data-cloud="{esc(cloud)}" data-alert-total="{alert_total}" data-latest-date="{esc(latest_date_attr)}" class="{row_class_str}">
<td>{i}</td>
<td><strong>{esc(permit)}</strong></td>
<td class="name-cell">{name_html}</td>
<td>{cloud_badge}</td>
<td class="col-num">{drive_link}</td>
<td class="col-num">{system_count}</td>
<td>{coverage_html}</td>
<td>{merged_alert_html}</td>
<td>{latest_html}</td>
<td class="col-num">{days_html}</td>
<td><span class="badge {badge_class}">{esc(badge_text)}</span></td>
</tr>'''

    html = f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8"/>
<meta content="width=device-width, initial-scale=1.0" name="viewport"/>
<title>建照監測追蹤報告</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft JhengHei",sans-serif;background:#f5f5f5;padding:15px;color:#333;line-height:1.4;font-size:13px}}
.container{{max-width:1600px;margin:0 auto;background:white;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,0.1)}}
.header{{background:linear-gradient(135deg,#dc2626,#991b1b);color:white;padding:25px;text-align:center}}
.header h1{{font-size:24px;margin-bottom:6px}}
.header .meta{{font-size:12px;opacity:0.9}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;padding:15px;background:#fafafa}}
.stat{{background:white;padding:12px;border-radius:8px;border-left:4px solid #dc2626;text-align:center}}
.stat .label{{font-size:10px;color:#666}}
.stat .value{{font-size:20px;font-weight:700;color:#dc2626}}
/* 圖例說明 section */
.legend-section{{background:#f0f7ff;border-top:3px solid #60a5fa;padding:0}}
.legend-toggle{{width:100%;background:none;border:none;padding:10px 15px;text-align:left;cursor:pointer;font-size:12px;font-weight:700;color:#1e40af;display:flex;align-items:center;gap:8px}}
.legend-toggle:hover{{background:#dbeafe}}
.toggle-arrow-legend{{transition:transform 0.2s;display:inline-block;font-style:normal}}
.legend-section.open .toggle-arrow-legend{{transform:rotate(90deg)}}
.legend-body{{display:none;padding:12px 15px 15px}}
.legend-section.open .legend-body{{display:block}}
.legend-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}}
.legend-block{{background:white;border-radius:6px;padding:10px;border:1px solid #bfdbfe}}
.legend-block-title{{font-size:11px;font-weight:700;color:#1e40af;margin-bottom:7px;padding-bottom:4px;border-bottom:1px solid #dbeafe}}
.legend-table{{width:100%;border-collapse:collapse;font-size:11px}}
.legend-table td{{padding:3px 5px;vertical-align:middle;color:#374151}}
.legend-color-cell{{width:16px;border-radius:2px}}
.legend-col-name{{font-weight:600;color:#1e40af;white-space:nowrap;padding-right:8px}}
/* 需要關注 section */
.attention-section{{background:#fff7ed;border-top:3px solid #dc2626;padding:0}}
.attention-toggle{{width:100%;background:none;border:none;padding:12px 15px;text-align:left;cursor:pointer;font-size:13px;font-weight:700;color:#991b1b;display:flex;align-items:center;gap:8px}}
.attention-toggle:hover{{background:#fee2e2}}
.attention-toggle .toggle-arrow{{transition:transform 0.2s;display:inline-block;font-style:normal}}
.attention-section.open .toggle-arrow{{transform:rotate(90deg)}}
.attention-body{{display:none;padding:12px 15px 15px}}
.attention-section.open .attention-body{{display:block}}
.attention-group{{margin-bottom:12px}}
.attention-group h4{{font-size:12px;color:#92400e;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid #fcd34d}}
.attention-cards{{display:flex;flex-wrap:wrap;gap:8px}}
.attention-card{{background:white;border-radius:6px;padding:8px 10px;min-width:160px;max-width:220px;border-left:3px solid #dc2626;box-shadow:0 1px 3px rgba(0,0,0,0.08)}}
.attention-card-alert{{border-left-color:#dc2626}}
.ac-permit{{font-size:10px;font-weight:700;color:#991b1b}}
.ac-name{{font-size:10px;color:#666;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.ac-summary{{font-size:12px;margin-top:4px}}
.ac-date{{font-size:9px;color:#999;margin-top:2px}}
.stale-table{{width:100%;border-collapse:collapse;font-size:11px}}
.stale-table th{{background:#fef3c7;padding:5px 8px;text-align:left;font-size:10px;color:#92400e}}
.stale-table td{{padding:4px 8px;border-bottom:1px solid #fef9e7}}
/* non-google */
.non-google{{background:#fef3c7;padding:15px;margin:0}}
.non-google h3{{font-size:14px;color:#92400e;margin-bottom:10px}}
.cloud-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}}
.cloud-card{{background:white;padding:10px;border-radius:6px;border:1px solid #fcd34d}}
.cloud-card h4{{font-size:12px;color:#92400e;margin-bottom:6px;display:flex;align-items:center;gap:5px}}
.cloud-card .icon{{font-size:16px}}
.cloud-card ul{{font-size:10px;color:#666;list-style:none;max-height:80px;overflow-y:auto}}
.cloud-card li{{padding:2px 0;border-bottom:1px solid #f5f5f5}}
.content{{padding:15px}}
.controls{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:15px;align-items:center;justify-content:space-between}}
.filter-group{{display:flex;gap:0;border:1px solid #e5e5e5;border-radius:8px;overflow:hidden}}
.filter-group .btn{{border:none;border-right:1px solid #e5e5e5;border-radius:0;padding:6px 12px;font-size:11px}}
.filter-group .btn:last-child{{border-right:none}}
.search{{padding:8px 12px;width:220px;border:2px solid #e5e5e5;border-radius:8px;font-size:13px}}
.search:focus{{outline:none;border-color:#dc2626}}
.btn{{padding:4px 8px;border:1px solid #e5e5e5;border-radius:4px;background:white;cursor:pointer;font-size:10px}}
.btn:hover{{border-color:#dc2626}}
.btn.active{{background:#dc2626;color:white;border-color:#dc2626}}
.table-wrap{{overflow-x:auto;border-radius:6px;border:1px solid #e5e5e5;max-height:800px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
thead{{background:#dc2626;color:white;position:sticky;top:0;z-index:10;box-shadow:0 4px 6px -1px rgba(0,0,0,0.1)}}
th{{padding:10px 8px;text-align:left;font-size:11px;cursor:pointer;white-space:nowrap}}
th:hover{{background:#b91c1c}}
th:nth-child(1){{width:35px}}
th:nth-child(11){{min-width:75px}}
td{{padding:10px 8px;border-bottom:1px solid #f0f0f0;vertical-align:top}}
td.col-num,th.col-num{{text-align:right}}
.empty-val{{color:#d1d5db}}
tr:hover{{background:#fafafa}}
tr.row-alert td:first-child{{border-left:3px solid #ef4444}}
tr.row-stale td:first-child{{border-left:3px solid #fbbf24}}
tr.row-alert.row-stale td:first-child{{border-left:3px solid #ef4444}}
.badge{{display:inline-block;padding:2px 6px;border-radius:3px;font-size:9px;font-weight:600;white-space:nowrap}}
.badge-success{{background:#dcfce7;color:#166534}}
.badge-info{{background:#dbeafe;color:#1e40af}}
.badge-warning{{background:#fef3c7;color:#92400e}}
.badge-danger{{background:#fee2e2;color:#991b1b}}
.badge-gray{{background:#f3f4f6;color:#6b7280}}
.badge-orange{{background:#ffedd5;color:#c2410c}}
.progress-wrapper{{display:flex;align-items:center;gap:6px;min-width:90px}}
.progress-text{{width:30px;text-align:right;font-size:11px;color:#4b5563;font-weight:600}}
.bar{{flex-grow:1;height:6px;background:#e5e7eb;border-radius:3px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:2px}}
a{{color:#dc2626;text-decoration:none;white-space:nowrap}}
a:hover{{text-decoration:underline}}
.days{{font-size:10px}}
.days-old{{color:#dc2626;font-weight:600}}
.days-recent{{color:#22c55e}}
.alert-merged{{font-size:11px;white-space:nowrap;cursor:help}}
.name-cell{{max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;color:#666}}
@media (max-width:768px) {{
  .search{{width:100%}}
  .col-cloud,.col-coverage{{display:none}}
}}
</style>
</head>
<body>
<div class="container">
<div class="header">
<h1>建照監測追蹤報告</h1>
<div class="meta">{now.strftime('%Y年%m月%d日 %H:%M')} | 自動生成</div>
</div>
<div class="stats">
<div class="stat" title="台北市政府列管的建案監測數量"><div class="label">監測建案總數</div><div class="value">{total}</div></div>
<div class="stat" title="所有報告都已上傳到究平安系統完成分析"><div class="label">已完成上傳</div><div class="value" style="color:#22c55e">{completed}</div></div>
<div class="stat" title="報告已上傳，AI 正在分析處理中"><div class="label">正在分析中</div><div class="value" style="color:#3b82f6">{in_progress}</div></div>
<div class="stat" title="雲端有報告但尚未上傳到究平安系統"><div class="label">尚未上傳</div><div class="value" style="color:#f59e0b">{not_uploaded}</div></div>
<div class="stat" title="雲端資料夾中沒有任何 PDF 報告"><div class="label">尚無監測資料</div><div class="value" style="color:#6b7280">{no_reports}</div></div>
<div class="stat" title="使用 SharePoint、Dropbox 等其他雲端服務"><div class="label">非 Google Drive</div><div class="value" style="color:#c2410c">{other_cloud}</div></div>
<div class="stat" title="同步或上傳過程中發生錯誤"><div class="label">異常</div><div class="value" style="color:#dc2626">{errors}</div></div>
</div>

<div class="legend-section" id="legendSection">
<button class="legend-toggle" onclick="toggleLegend()">
<i class="toggle-arrow-legend">▶</i> 📖 圖例說明
</button>
<div class="legend-body" id="legendBody">
<div class="legend-grid">
<div class="legend-block">
<div class="legend-block-title">同步狀態說明</div>
<table class="legend-table">
<tr><td><span class="badge badge-success">已完成</span></td><td>所有雲端報告皆已上傳並完成分析</td></tr>
<tr><td><span class="badge badge-info">分析中</span></td><td>報告已上傳，AI 正在分析處理中，請稍候</td></tr>
<tr><td><span class="badge badge-warning">待上傳</span></td><td>雲端有新報告，尚未上傳至究平安系統，需盡快處理</td></tr>
<tr><td><span class="badge badge-gray">無資料</span></td><td>雲端資料夾目前沒有任何 PDF 報告</td></tr>
<tr><td><span class="badge badge-danger">異常</span></td><td>同步或上傳過程中發生錯誤，請聯絡技術人員</td></tr>
</table>
</div>
<div class="legend-block">
<div class="legend-block-title">列顏色說明</div>
<table class="legend-table">
<tr><td class="legend-color-cell" style="background:#fff1f2;border-left:3px solid #dc2626;">&nbsp;&nbsp;&nbsp;&nbsp;</td><td><strong>紅色底</strong>：該工地有警戒值或行動值，需優先關注</td></tr>
<tr><td class="legend-color-cell" style="background:#fefce8;border-left:3px solid #f59e0b;">&nbsp;&nbsp;&nbsp;&nbsp;</td><td><strong>黃色底</strong>：報告超過 30 天未更新，請確認現場狀況</td></tr>
</table>
</div>
<div class="legend-block">
<div class="legend-block-title">欄位說明</div>
<table class="legend-table">
<tr><td class="legend-col-name">雲端報告數</td><td>Google Drive 上該工地的 PDF 報告總數（可點擊開啟資料夾）</td></tr>
<tr><td class="legend-col-name">已分析數</td><td>已上傳至究平安系統並完成 AI 分析的報告數量</td></tr>
<tr><td class="legend-col-name">分析進度</td><td>已分析數 ÷ 雲端報告數，進度條顯示完成比例</td></tr>
<tr><td class="legend-col-name">警戒紀錄</td><td>⚠️ 警戒值 / 🚨 行動值 / 🔴 超越行動值的觸發次數</td></tr>
<tr><td class="legend-col-name">更新間隔</td><td>最近一份報告距今天數，超過 30 天會以紅字標示</td></tr>
</table>
</div>
</div>
</div>
</div>

<div class="attention-section" id="attentionSection">
<button class="attention-toggle" onclick="toggleAttention()">
<i class="toggle-arrow">▶</i> ⚠️ 需要處理 — {len(alert_permits)} 個工地有警戒值，{len(stale_permits)} 個工地報告超過 30 天未更新
</button>
<div class="attention-body">
<div class="attention-group">
<h4>有警戒值的建案 ({len(alert_permits)} 個)</h4>
<div class="attention-cards">{attention_alert_cards if attention_alert_cards else '<span style="font-size:11px;color:#999">無</span>'}</div>
</div>
<div class="attention-group">
<h4>報告過期的建案 (超過 30 天未更新, {len(stale_permits)} 個)</h4>
<div style="max-height:300px;overflow-y:auto">
<table class="stale-table">
<thead><tr><th>建照字號</th><th>建案名稱</th><th>距今</th></tr></thead>
<tbody>{attention_stale_rows if attention_stale_rows else '<tr><td colspan="3" style="color:#999;font-size:11px;padding:6px">無</td></tr>'}</tbody>
</table>
</div>
</div>
</div>
</div>

<div class="non-google">
<h3>⚠️ 使用非 Google Drive 的建照 ({other_cloud} 個)</h3>
<div class="cloud-grid">{cloud_cards_html}</div>
</div>

<div class="content">
<div class="controls">
<input type="text" class="search" id="search" placeholder="搜尋建照號碼或建案名稱..." onkeyup="filterTable()">
<div class="filter-group">
<button class="btn active" onclick="filterStatus(this,'')">全部</button>
<button class="btn" onclick="filterStatus(this,'completed')">已完成</button>
<button class="btn" onclick="filterStatus(this,'in_progress')">分析中</button>
<button class="btn" onclick="filterStatus(this,'not_uploaded')">待上傳</button>
<button class="btn" onclick="filterStatus(this,'other_cloud')">非 Google Drive</button>
<button class="btn" onclick="filterStatus(this,'needs_attention')">需要處理</button>
</div>
</div>
<div class="table-wrap">
<table id="dataTable">
<thead>
<tr>
<th onclick="sortTable(0)">#</th>
<th onclick="sortTable(1)">建照號碼</th>
<th onclick="sortTable(2)">工地名稱</th>
<th onclick="sortTable(3)" class="col-cloud">資料來源</th>
<th onclick="sortTable(4)" class="col-num">雲端報告數</th>
<th onclick="sortTable(5)" class="col-num">已分析數</th>
<th onclick="sortTable(6)" class="col-coverage">分析進度</th>
<th onclick="sortTable(7)">警戒紀錄</th>
<th onclick="sortTable(8)">最近更新</th>
<th onclick="sortTable(9)" class="col-num">更新間隔</th>
<th onclick="sortTable(10)">同步狀態</th>
</tr>
</thead>
<tbody>{rows_html}
<tr id="emptyStateRow" style="display:none"><td colspan="11" style="text-align:center;padding:40px;color:#9ca3af"><div style="font-size:24px;margin-bottom:8px">🔍</div>找不到符合條件的建案紀錄</td></tr>
</tbody>
</table>
</div>
</div>
</div>

<script>
(function() {{
  // Dynamic date calculation
  const today = new Date();
  today.setHours(0,0,0,0);
  document.querySelectorAll('.days[data-date]').forEach(function(el) {{
    const raw = el.getAttribute('data-date');
    if (!raw || raw === '-') {{ el.textContent = '-'; return; }}
    const parts = raw.split('-');
    if (parts.length !== 3) {{ el.textContent = raw; return; }}
    const d = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
    const diff = Math.floor((today - d) / 86400000);
    el.textContent = diff + ' 天';
    if (diff > 30) {{
      el.classList.add('days-old');
    }} else if (diff <= 7) {{
      el.classList.add('days-recent');
    }}
  }});

  // Mark stale rows dynamically
  document.querySelectorAll('#dataTable tbody tr').forEach(function(row) {{
    const latestDate = row.getAttribute('data-latest-date');
    const status = row.getAttribute('data-status');
    if (latestDate && latestDate !== '-' && status !== 'no_reports') {{
      const parts = latestDate.split('-');
      if (parts.length === 3) {{
        const d = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
        const diff = Math.floor((today - d) / 86400000);
        if (diff > 30) row.classList.add('row-stale');
      }}
    }}
  }});
}})();

let currentFilter = '';
function filterTable() {{
  const search = document.getElementById('search').value.toLowerCase();
  const rows = document.querySelectorAll('#dataTable tbody tr:not(#emptyStateRow)');
  const today = new Date();
  today.setHours(0,0,0,0);
  let visibleCount = 0;
  rows.forEach(function(row) {{
    const permit = row.cells[1].textContent.toLowerCase();
    const name = row.cells[2].textContent.toLowerCase();
    const status = row.dataset.status;
    const cloud = row.dataset.cloud;
    const alertTotal = parseInt(row.dataset.alertTotal || '0');
    const matchSearch = permit.includes(search) || name.includes(search);
    let matchStatus = true;
    if (currentFilter === 'other_cloud') {{
      matchStatus = cloud !== 'Google Drive';
    }} else if (currentFilter === 'needs_attention') {{
      let isStale = false;
      const ld = row.getAttribute('data-latest-date');
      if (ld && ld !== '-' && status !== 'no_reports') {{
        const parts = ld.split('-');
        if (parts.length === 3) {{
          const d = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
          isStale = Math.floor((today - d) / 86400000) > 30;
        }}
      }}
      matchStatus = alertTotal > 0 || isStale;
    }} else if (currentFilter) {{
      matchStatus = status === currentFilter;
    }}
    const visible = matchSearch && matchStatus;
    row.style.display = visible ? '' : 'none';
    if (visible) visibleCount++;
  }});
  const esr = document.getElementById('emptyStateRow'); if(esr) esr.style.display = visibleCount === 0 ? 'table-row' : 'none';
}}
function filterStatus(btn, status) {{
  currentFilter = status;
  document.querySelectorAll('.btn').forEach(function(b) {{ b.classList.remove('active'); }});
  btn.classList.add('active');
  filterTable();
}}
function sortTable(n) {{
  const table = document.getElementById('dataTable');
  const rows = Array.from(table.rows).slice(1);
  const dir = table.dataset.sortDir === 'asc' ? -1 : 1;
  table.dataset.sortDir = dir === 1 ? 'asc' : 'desc';
  rows.sort(function(a, b) {{
    let x = a.cells[n].textContent;
    let y = b.cells[n].textContent;
    if (!isNaN(parseFloat(x)) && !isNaN(parseFloat(y))) {{
      return (parseFloat(x) - parseFloat(y)) * dir;
    }}
    return x.localeCompare(y, 'zh-TW') * dir;
  }});
  rows.forEach(function(row) {{ table.tBodies[0].appendChild(row); }});
}}
function toggleAttention() {{
  const sec = document.getElementById('attentionSection');
  sec.classList.toggle('open');
}}
function toggleLegend() {{
  const sec = document.getElementById('legendSection');
  sec.classList.toggle('open');
}}
</script>
</body>
</html>'''

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(OUTPUT_HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  已生成: {OUTPUT_HTML}")


def generate_csv_report(permit_data: Dict[str, dict], non_google: List[dict], alert_data: Dict[str, dict] = None, permit_names: Dict[str, str] = None):
    """生成 CSV 報告"""
    print("📄 生成 CSV 報告...")

    if alert_data is None:
        alert_data = {}
    if permit_names is None:
        permit_names = {}

    non_google_set = {item['permit']: item['cloud'] for item in non_google}

    sorted_permits = sorted(permit_data.keys(), key=lambda x: (
        int(re.search(r'(\d{2,3})建字', x).group(1)) if re.search(r'(\d{2,3})建字', x) else 0,
        int(re.search(r'第(\d+)號', x).group(1)) if re.search(r'第(\d+)號', x) else 0
    ), reverse=True)

    lines = ['序號,建照字號,建案名稱,雲端服務,Drive PDF,系統 PDF,覆蓋率,警戒次數,行動次數,Alert次數,最近警戒日期,最新報告,距今天數,狀態']

    for i, permit in enumerate(sorted_permits, 1):
        data = permit_data[permit]
        cloud = non_google_set.get(permit, 'Google Drive')
        drive = data.get('drive_count', 0)
        system = data.get('system_count', 0)
        coverage = f"{min(100, int(system/drive*100))}%" if drive > 0 and system > 0 else '-'
        latest = data.get('latest_report', '')[:10] if data.get('latest_report') else ''
        days = data.get('days_since_update', '')
        status = data.get('status', 'unknown')

        # 建案名稱
        building_name = permit_names.get(permit, '')

        # 警戒值
        permit_alert = alert_data.get(permit, {})
        warning = permit_alert.get('warning_count', 0)
        action = permit_alert.get('action_count', 0)
        alert = permit_alert.get('alert_count', 0)
        latest_alert = permit_alert.get('latest_alert_date', '')[:10] if permit_alert.get('latest_alert_date') else ''

        lines.append(f'{i},"{permit}","{building_name}","{cloud}",{drive},{system},{coverage},{warning},{action},{alert},{latest_alert},{latest},{days},{status}')

    with open(OUTPUT_CSV, 'w', encoding='utf-8-sig') as f:
        f.write('\n'.join(lines))
    print(f"  已生成: {OUTPUT_CSV}")


def main():
    """主程式"""
    print("=" * 50)
    print("建照監測追蹤報告生成工具")
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

        # 計算最新報告日期（使用 Google Drive 的修改時間）
        latest_report = info.get('latest_pdf', '')

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

    # 5. 載入警戒資料和建案名稱
    alert_data, permit_names = load_alert_data()

    # 6. 儲存資料
    with open(MAPPING_JSON, 'w', encoding='utf-8') as f:
        json.dump(permit_data, f, indent=2, ensure_ascii=False)

    with open(NON_GOOGLE_JSON, 'w', encoding='utf-8') as f:
        json.dump(non_google, f, indent=2, ensure_ascii=False)

    # 7. 生成報告
    generate_html_report(permit_data, non_google, alert_data, permit_names)
    generate_csv_report(permit_data, non_google, alert_data, permit_names)

    elapsed = time.time() - start_time
    print(f"\n✅ 報告生成完成！耗時 {elapsed:.1f} 秒")
    print(f"   - HTML: {OUTPUT_HTML}")
    print(f"   - CSV: {OUTPUT_CSV}")


if __name__ == '__main__':
    main()
