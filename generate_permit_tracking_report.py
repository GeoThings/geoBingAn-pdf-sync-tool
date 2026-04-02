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

    # 初始化所有資料夾的計數
    for permit in permit_folders:
        permit_folders[permit]['pdf_count'] = 0
        permit_folders[permit]['latest_pdf'] = ''

    page_token = None
    total_pdfs = 0
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

            for f in results.get('files', []):
                parents = f.get('parents', [])
                if not parents:
                    continue
                parent_id = parents[0]
                permit = folder_id_to_permit.get(parent_id)
                if permit:
                    permit_folders[permit]['pdf_count'] += 1
                    mod_time = f.get('modifiedTime', '')
                    if mod_time > permit_folders[permit]['latest_pdf']:
                        permit_folders[permit]['latest_pdf'] = mod_time
                    total_pdfs += 1

            page_token = results.get('nextPageToken')
            if not page_token:
                break
        except HttpError as e:
            print(f"    ❌ PDF 掃描失敗: {e}")
            break

    print(f"    統計完成: {total_pdfs} 個 PDF 分布在 {len(permit_folders)} 個資料夾")

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
    print("\n📊 生成 HTML 報告...")

    if alert_data is None:
        alert_data = {}
    if permit_names is None:
        permit_names = {}

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
        permits_html = ''.join([f'<li>{p}</li>' for p in permits[:20]])
        if len(permits) > 20:
            permits_html += f'<li>...還有 {len(permits) - 20} 個</li>'
        cloud_cards_html += f'''
<div class="cloud-card">
<h4><span class="icon">{icon}</span> {cloud} ({len(permits)})</h4>
<ul>{permits_html}</ul>
</div>'''

    # 建立非 Google 查詢表
    non_google_set = {item['permit']: item['cloud'] for item in non_google}

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
            'completed': ('✓ 完成', 'badge-success'),
            'in_progress': ('處理中', 'badge-info'),
            'not_uploaded': ('未上傳', 'badge-warning'),
            'no_reports': ('無報告', 'badge-gray'),
            'error': ('錯誤', 'badge-danger')
        }
        badge_text, badge_class = status_badges.get(status, ('未知', 'badge-gray'))

        # 雲端 badge
        if cloud == 'Google Drive':
            cloud_badge = ''
        else:
            cloud_badge = f'<span class="badge badge-orange">{cloud}</span>'

        # 覆蓋率
        if drive_count > 0 and system_count > 0:
            coverage = min(100, int(system_count / drive_count * 100))
            bar_color = '#22c55e' if coverage >= 80 else '#f59e0b' if coverage >= 50 else '#dc2626'
            coverage_html = f'{coverage}% <span class="bar"><span class="bar-fill" style="width:{coverage}%;background:{bar_color}"></span></span>'
        else:
            coverage_html = '-'

        # 天數
        if days != '' and days is not None:
            days_class = 'days-old' if int(days) > 30 else 'days-recent' if int(days) <= 7 else ''
            days_html = f'<span class="days {days_class}">{days} 天</span>'
        else:
            days_html = '-'

        # 連結
        if folder_id:
            drive_link = f'<a href="https://drive.google.com/drive/folders/{folder_id}" target="_blank">{drive_count}</a>'
        else:
            drive_link = str(drive_count)

        # 最新報告日期
        latest_html = latest[:10] if latest else '-'

        # 建案名稱
        building_name = permit_names.get(permit, '')
        # 截斷過長的名稱
        if len(building_name) > 25:
            name_html = f'<span title="{building_name}">{building_name[:25]}...</span>'
        else:
            name_html = building_name if building_name else '-'

        # 警戒/行動值
        permit_alert = alert_data.get(permit, {})
        warning_count = permit_alert.get('warning_count', 0)
        action_count = permit_alert.get('action_count', 0)
        alert_count = permit_alert.get('alert_count', 0)
        latest_alert_date = permit_alert.get('latest_alert_date', '')

        # 警戒值顯示（有則顯示數字，無則顯示 -）
        if warning_count > 0:
            warning_html = f'<span class="alert-warning">{warning_count}</span>'
        else:
            warning_html = '-'

        if action_count > 0:
            action_html = f'<span class="alert-action">{action_count}</span>'
        else:
            action_html = '-'

        if alert_count > 0:
            alert_html = f'<span class="alert-critical">{alert_count}</span>'
        else:
            alert_html = '-'

        # 最近警戒日期
        if latest_alert_date and (warning_count > 0 or action_count > 0 or alert_count > 0):
            alert_date_html = latest_alert_date[:10]
        else:
            alert_date_html = '-'

        rows_html += f'''
<tr data-status="{status}" data-cloud="{cloud}">
<td>{i}</td>
<td><strong>{permit}</strong></td>
<td class="name-cell">{name_html}</td>
<td>{cloud_badge}</td>
<td>{drive_link}</td>
<td>{system_count}</td>
<td>{coverage_html}</td>
<td>{warning_html}</td>
<td>{action_html}</td>
<td>{alert_html}</td>
<td>{alert_date_html}</td>
<td>{latest_html}</td>
<td>{days_html}</td>
<td><span class="badge {badge_class}">{badge_text}</span></td>
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
.non-google{{background:#fef3c7;padding:15px;margin:0}}
.non-google h3{{font-size:14px;color:#92400e;margin-bottom:10px}}
.cloud-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}}
.cloud-card{{background:white;padding:10px;border-radius:6px;border:1px solid #fcd34d}}
.cloud-card h4{{font-size:12px;color:#92400e;margin-bottom:6px;display:flex;align-items:center;gap:5px}}
.cloud-card .icon{{font-size:16px}}
.cloud-card ul{{font-size:10px;color:#666;list-style:none;max-height:80px;overflow-y:auto}}
.cloud-card li{{padding:2px 0;border-bottom:1px solid #f5f5f5}}
.content{{padding:15px}}
.controls{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px;align-items:center}}
.search{{padding:6px 10px;width:180px;border:2px solid #e5e5e5;border-radius:5px;font-size:12px}}
.search:focus{{outline:none;border-color:#dc2626}}
.btn{{padding:4px 8px;border:1px solid #e5e5e5;border-radius:4px;background:white;cursor:pointer;font-size:10px}}
.btn:hover{{border-color:#dc2626}}
.btn.active{{background:#dc2626;color:white;border-color:#dc2626}}
.table-wrap{{overflow-x:auto;border-radius:6px;border:1px solid #e5e5e5;max-height:600px}}
table{{width:100%;border-collapse:collapse;font-size:11px}}
thead{{background:#dc2626;color:white;position:sticky;top:0;z-index:10}}
th{{padding:8px 5px;text-align:left;font-size:10px;cursor:pointer;white-space:nowrap}}
th:hover{{background:#b91c1c}}
td{{padding:6px 5px;border-bottom:1px solid #f0f0f0;vertical-align:top}}
tr:hover{{background:#fafafa}}
.badge{{display:inline-block;padding:2px 6px;border-radius:3px;font-size:9px;font-weight:600;white-space:nowrap}}
.badge-success{{background:#dcfce7;color:#166534}}
.badge-info{{background:#dbeafe;color:#1e40af}}
.badge-warning{{background:#fef3c7;color:#92400e}}
.badge-danger{{background:#fee2e2;color:#991b1b}}
.badge-gray{{background:#f3f4f6;color:#6b7280}}
.badge-orange{{background:#ffedd5;color:#c2410c}}
.bar{{width:40px;height:4px;background:#e5e5e5;border-radius:2px;display:inline-block;vertical-align:middle}}
.bar-fill{{height:100%;border-radius:2px}}
a{{color:#dc2626;text-decoration:none}}
.days{{font-size:10px}}
.days-old{{color:#dc2626;font-weight:600}}
.days-recent{{color:#22c55e}}
.alert-warning{{color:#f59e0b;font-weight:600}}
.alert-action{{color:#dc2626;font-weight:600}}
.alert-critical{{color:#7c2d12;font-weight:700;background:#fee2e2;padding:2px 6px;border-radius:3px}}
.name-cell{{max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;color:#666}}
</style>
</head>
<body>
<div class="container">
<div class="header">
<h1>建照監測追蹤報告</h1>
<div class="meta">{now.strftime('%Y年%m月%d日 %H:%M')} | 自動生成</div>
</div>
<div class="stats">
<div class="stat"><div class="label">總建照</div><div class="value">{total}</div></div>
<div class="stat"><div class="label">✓ 完成</div><div class="value" style="color:#22c55e">{completed}</div></div>
<div class="stat"><div class="label">處理中</div><div class="value" style="color:#3b82f6">{in_progress}</div></div>
<div class="stat"><div class="label">未上傳</div><div class="value" style="color:#f59e0b">{not_uploaded}</div></div>
<div class="stat"><div class="label">無報告</div><div class="value" style="color:#6b7280">{no_reports}</div></div>
<div class="stat"><div class="label">其他雲端</div><div class="value" style="color:#c2410c">{other_cloud}</div></div>
<div class="stat"><div class="label">錯誤</div><div class="value" style="color:#dc2626">{errors}</div></div>
</div>

<div class="non-google">
<h3>⚠️ 使用非 Google Drive 的建照 ({other_cloud} 個)</h3>
<div class="cloud-grid">{cloud_cards_html}</div>
</div>

<div class="content">
<div class="controls">
<input type="text" class="search" id="search" placeholder="搜尋建照號碼..." onkeyup="filterTable()">
<button class="btn active" onclick="filterStatus('')">全部</button>
<button class="btn" onclick="filterStatus('completed')">完成</button>
<button class="btn" onclick="filterStatus('in_progress')">處理中</button>
<button class="btn" onclick="filterStatus('not_uploaded')">未上傳</button>
<button class="btn" onclick="filterStatus('other_cloud')">其他雲端</button>
</div>
<div class="table-wrap">
<table id="dataTable">
<thead>
<tr>
<th onclick="sortTable(0)">#</th>
<th onclick="sortTable(1)">建照字號</th>
<th onclick="sortTable(2)">建案名稱</th>
<th onclick="sortTable(3)">雲端</th>
<th onclick="sortTable(4)">Drive PDF</th>
<th onclick="sortTable(5)">系統 PDF</th>
<th onclick="sortTable(6)">覆蓋率</th>
<th onclick="sortTable(7)">警戒</th>
<th onclick="sortTable(8)">行動</th>
<th onclick="sortTable(9)">Alert</th>
<th onclick="sortTable(10)">最近警戒</th>
<th onclick="sortTable(11)">最新報告</th>
<th onclick="sortTable(12)">距今</th>
<th onclick="sortTable(13)">狀態</th>
</tr>
</thead>
<tbody>{rows_html}</tbody>
</table>
</div>
</div>
</div>

<script>
let currentFilter = '';
function filterTable() {{
  const search = document.getElementById('search').value.toLowerCase();
  const rows = document.querySelectorAll('#dataTable tbody tr');
  rows.forEach(row => {{
    const permit = row.cells[1].textContent.toLowerCase();
    const status = row.dataset.status;
    const cloud = row.dataset.cloud;
    const matchSearch = permit.includes(search);
    const matchStatus = !currentFilter ||
      (currentFilter === 'other_cloud' ? cloud !== 'Google Drive' : status === currentFilter);
    row.style.display = matchSearch && matchStatus ? '' : 'none';
  }});
}}
function filterStatus(status) {{
  currentFilter = status;
  document.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  filterTable();
}}
function sortTable(n) {{
  const table = document.getElementById('dataTable');
  const rows = Array.from(table.rows).slice(1);
  const dir = table.dataset.sortDir === 'asc' ? -1 : 1;
  table.dataset.sortDir = dir === 1 ? 'asc' : 'desc';
  rows.sort((a, b) => {{
    let x = a.cells[n].textContent;
    let y = b.cells[n].textContent;
    if (!isNaN(parseFloat(x)) && !isNaN(parseFloat(y))) {{
      return (parseFloat(x) - parseFloat(y)) * dir;
    }}
    return x.localeCompare(y, 'zh-TW') * dir;
  }});
  rows.forEach(row => table.tBodies[0].appendChild(row));
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
