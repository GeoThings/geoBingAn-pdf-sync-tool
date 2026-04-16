#!/usr/bin/env python3
"""
建案交叉比對工具 — 從 6 個來源匹配建照號碼與建案名稱/地址/階段

來源：
1. 政府 PDF（建照號碼 + 承造人）
2. Drive 來源資料夾名稱
3. Drive PDF 檔名
4. API construction-projects（580 筆，含地址/階段/感測器）
5. API construction-reports（file_name → 建照號碼橋樑）
6. alert_data.csv

產出：state/permit_registry.json
"""
import json
import os
import re
import sys
import csv
import requests
from datetime import datetime
from typing import Dict, Optional
from collections import Counter

# 載入設定
try:
    from config import (JWT_TOKEN, REFRESH_TOKEN, GEOBINGAN_REFRESH_URL,
                        GROUP_ID, SHARED_DRIVE_ID)
except ImportError:
    print("❌ 需要 config.py")
    sys.exit(1)

from jwt_auth import get_valid_token
from filename_date_parser import parse_date_from_filename
from google.oauth2 import service_account
from googleapiclient.discovery import build

REGISTRY_FILE = './state/permit_registry.json'
ALERT_CSV = './state/alert_data.csv'
SERVICE_ACCOUNT_FILE = os.environ.get('GOOGLE_CREDENTIALS', './credentials.json')


def get_api_token():
    global JWT_TOKEN
    token, was_refreshed, new_refresh = get_valid_token(JWT_TOKEN, REFRESH_TOKEN, GEOBINGAN_REFRESH_URL)
    if was_refreshed:
        JWT_TOKEN = token
        try:
            from config import update_jwt_token
            update_jwt_token(token, new_refresh)
        except Exception as e:
            print(f"⚠️  無法更新 .env Token: {e}")
    return token


def load_existing_registry() -> dict:
    if os.path.exists(REGISTRY_FILE):
        with open(REGISTRY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def normalize_permit(raw: str) -> Optional[str]:
    """標準化建照號碼格式"""
    m = re.search(r'(\d{2,3})\s*建\s*字?\s*第?\s*0*(\d{3,5})\s*號?', raw)
    if m:
        year = m.group(1)
        num = m.group(2).zfill(4)
        return f'{year}建字第{num}號'
    return None


def extract_name_from_text(text: str) -> str:
    """從文字中提取建案名稱（清理通用詞）"""
    name = text
    name = re.sub(r'\d{2,3}\s*建\s*字?\s*第?\s*\d{3,5}\s*號?\s*', '', name)
    name = re.sub(r'建照字號', '', name)
    name = re.sub(r'(新建工程|建案|工程|監測案|監測數據|雲端資料庫)\s*$', '', name)
    name = re.sub(r'[（(]本網站由.*$', '', name)
    name = re.sub(r'[（(]該網址?由.*$', '', name)
    name = re.sub(r'[（(]資料庫係由.*$', '', name)
    name = re.sub(r'安全觀測$', '', name)
    name = name.strip(' -_/()')
    if len(name) < 2:
        return ''
    return name


# ==================== 來源 1: 政府 PDF ====================
def fetch_gov_pdf_data(city: dict = None) -> Dict[str, dict]:
    """從政府 PDF 取得建照號碼 + 承造人/監造人"""
    print("📄 來源 1: 政府 PDF...")
    from sync_permits import PermitSync
    ps = PermitSync(city=city)
    pdf_path = ps.download_pdf_list()
    mapping = ps.parse_pdf_list(pdf_path)

    # 從 PDF 原文解析承造人
    import PyPDF2
    with open(pdf_path, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        all_text = ''.join(p.extract_text() or '' for p in reader.pages)

    results = {}
    for permit in mapping:
        norm = normalize_permit(permit)
        if norm:
            results[norm] = {
                'source_url': mapping[permit],
                'source_folder_id': ps.extract_folder_id_from_url(mapping[permit]),
            }

    print(f"  {len(results)} 個建照")
    return results


# ==================== 來源 2: Drive 來源資料夾名稱 ====================
def fetch_source_folder_names(gov_data: dict, drive_service) -> Dict[str, str]:
    """從來源 Google Drive 資料夾名稱提取建案名稱"""
    print("📂 來源 2: Drive 來源資料夾名稱...")
    names = {}
    for permit, info in gov_data.items():
        fid = info.get('source_folder_id')
        if not fid:
            continue
        try:
            folder = drive_service.files().get(
                fileId=fid, fields='name', supportsAllDrives=True
            ).execute()
            raw_name = folder.get('name', '')
            clean = extract_name_from_text(raw_name)
            if clean:
                names[permit] = clean
        except Exception:
            continue

    print(f"  {len(names)} 個有名稱")
    return names


# ==================== 來源 3: Drive PDF 檔名 ====================
def fetch_drive_pdf_names(drive_service) -> Dict[str, dict]:
    """批次掃描 Shared Drive 所有 PDF，提取建案名稱"""
    print("📁 來源 3: Drive PDF 檔名...")

    from drive_utils import list_top_level_folders, list_all_subfolders, build_folder_resolver

    # 頂層資料夾 ID → 建照號碼
    raw_folders = list_top_level_folders(drive_service, SHARED_DRIVE_ID)
    folders = {}
    for f in raw_folders:
        norm = normalize_permit(f['name'])
        if norm:
            folders[f['id']] = norm

    # 子資料夾層級解析
    all_subfolders = list_all_subfolders(drive_service, SHARED_DRIVE_ID)
    resolve_permit = build_folder_resolver(folders, all_subfolders)
    for fid in all_subfolders:
        p = resolve_permit(fid)
        if p:
            folders[fid] = p
    print(f"  含子資料夾共 {len(folders)} 個資料夾對應")

    # 批次掃描所有 PDF
    permit_files = {}  # permit → [filenames]
    page_token = None
    while True:
        try:
            results = drive_service.files().list(
                q="mimeType='application/pdf' and trashed=false",
                corpora='drive', driveId=SHARED_DRIVE_ID,
                includeItemsFromAllDrives=True, supportsAllDrives=True,
                fields='nextPageToken, files(name, parents)',
                pageSize=1000, pageToken=page_token
            ).execute()
            for f in results.get('files', []):
                parents = f.get('parents', [])
                if parents:
                    permit = folders.get(parents[0])
                    if permit:
                        if permit not in permit_files:
                            permit_files[permit] = []
                        permit_files[permit].append(f['name'])
            page_token = results.get('nextPageToken')
            if not page_token:
                break
        except Exception:
            break

    # 從檔名提取名稱（投票制：最常出現的名稱）
    from generate_permit_tracking_report import extract_name_from_filename
    names = {}
    for permit, files in permit_files.items():
        counts = Counter()
        for fn in files:
            name = extract_name_from_filename(fn)
            if name:
                counts[name] += 1
        if counts:
            names[permit] = {
                'name': counts.most_common(1)[0][0],
                'pdf_count': len(files),
            }

    print(f"  {len(names)} 個有名稱（共 {len(permit_files)} 個建照）")
    return names


# ==================== 來源 4: API construction-projects ====================
def fetch_api_projects() -> list:
    """從 API 取得所有 construction-projects"""
    print("🌐 來源 4: API construction-projects...")
    token = get_api_token()
    headers = {'Authorization': f'Bearer {token}', 'X-Current-Group': GROUP_ID}

    all_projects = []
    page = 1
    while True:
        r = requests.get(
            f'https://riskmap.today/api/groups/{GROUP_ID}/construction-projects/?page={page}&page_size=100',
            headers=headers, timeout=15
        )
        if r.status_code != 200:
            break
        data = r.json()
        all_projects.extend(data.get('results', []))
        if not data.get('next'):
            break
        page += 1

    print(f"  {len(all_projects)} 筆")
    return all_projects


# ==================== 來源 5: API reports (橋樑) ====================
def fetch_api_report_categories() -> Dict[str, str]:
    """從 API reports 的 file_name 建立 category → permit 的橋樑"""
    print("📡 來源 5: API construction-reports（建立橋樑）...")
    token = get_api_token()
    headers = {'Authorization': f'Bearer {token}'}

    # 分頁取得所有 reports
    all_reports = []
    page = 1
    auth_retries = 0
    while True:
        r = requests.get(
            f'https://riskmap.today/api/reports/construction-reports/?group_id={GROUP_ID}&page={page}&page_size=100',
            headers=headers, timeout=30
        )
        if r.status_code == 401:
            auth_retries += 1
            if auth_retries > 2:
                print("  ❌ 401 重試超過上限，停止")
                break
            token = get_api_token()
            headers = {'Authorization': f'Bearer {token}'}
            continue
        auth_retries = 0
        if r.status_code != 200:
            break
        data = r.json()
        results = data.get('results', [])
        if not results:
            break
        all_reports.extend(results)
        if not data.get('next'):
            break
        page += 1
        if page % 50 == 0:
            print(f"    第 {page} 頁...")

    print(f"  {len(all_reports)} 筆報告")

    # 從 file_name 提取建照號碼
    permit_from_reports = {}  # file_name → permit
    for report in all_reports:
        fn = report.get('file_name') or ''
        m = re.search(r'(\d{2,3}建字第\d{3,5}號)', fn)
        if m:
            permit_from_reports[fn] = normalize_permit(m.group(1))

    print(f"  {len(permit_from_reports)} 個可對應建照")
    return permit_from_reports


# ==================== 來源 6: API construction-alerts（即時） ====================
def fetch_live_alerts() -> Dict[str, list]:
    """從 API 取得即時警示資料（取代靜態 alert_data.csv）"""
    print("🚨 來源 6: API construction-alerts（即時）...")
    token = get_api_token()
    headers = {'Authorization': f'Bearer {token}', 'X-Current-Group': GROUP_ID}

    try:
        r = requests.get(
            f'https://riskmap.today/api/groups/{GROUP_ID}/construction-alerts/',
            headers=headers, timeout=15
        )
        if r.status_code != 200:
            print(f"  API 錯誤: {r.status_code}")
            return {}

        data = r.json()
        summary = data.get('summary', {})
        alerts = data.get('alerts', [])

        print(f"  危險: {summary.get('danger', 0)}, 警戒: {summary.get('warning', 0)}")
        print(f"  共 {len(alerts)} 筆警示")

        # 按 project 分組
        by_project = {}  # project_name → [alerts]
        for alert in alerts:
            project = alert.get('project', '')
            if project:
                if project not in by_project:
                    by_project[project] = []
                by_project[project].append({
                    'level': alert.get('level', ''),
                    'tone': alert.get('tone', ''),
                    'detail': alert.get('detail', ''),
                    'date': alert.get('updatedAt', ''),
                    'sensor': alert.get('sensor', ''),
                })

        return by_project

    except Exception as e:
        print(f"  錯誤: {e}")
        return {}


# ==================== 主程式：交叉比對 ====================
def build_registry(city: dict = None):
    if city:
        global SHARED_DRIVE_ID, GROUP_ID
        SHARED_DRIVE_ID = city.get('shared_drive_id') or SHARED_DRIVE_ID
        GROUP_ID = city.get('group_id') or GROUP_ID
    city_name = city.get('name', '') if city else ''
    print("=" * 60)
    print(f"🔍 建案交叉比對工具{f' ({city_name})' if city_name else ''}")
    print("=" * 60)

    # 初始化 Drive
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/drive.readonly'])
    drive_service = build('drive', 'v3', credentials=creds)

    # 載入現有 registry
    registry = load_existing_registry()
    print(f"現有 registry: {len(registry)} 筆\n")

    # 取得所有來源
    gov_data = fetch_gov_pdf_data(city=city)
    source_names = fetch_source_folder_names(gov_data, drive_service)
    drive_names = fetch_drive_pdf_names(drive_service)
    api_projects = fetch_api_projects()
    report_permits = fetch_api_report_categories()
    live_alerts = fetch_live_alerts()

    # 建立 API project 關鍵字索引（用於模糊匹配，去重相似名稱）
    api_project_index = {}  # keyword → project info
    for p in api_projects:
        name = p.get('project_name', '')
        if not name:
            continue
        # 去重：如果已有名稱是此名稱的子字串（或反過來），跳過較短的
        skip = False
        to_remove = []
        for existing_name in api_project_index:
            if name in existing_name:
                skip = True  # 已有更完整的名稱
                break
            if existing_name in name:
                to_remove.append(existing_name)  # 新名稱更完整，移除舊的
        for rm in to_remove:
            del api_project_index[rm]
        if skip:
            continue
        alert = p.get('alert_status', {})
        api_project_index[name] = {
            'address': p.get('location_address', ''),
            'stage': p.get('stage', ''),
            'sensors': p.get('sensor_count', 0),
            'coordinates': p.get('construction_coordinates'),
            'alert_label': alert.get('label', '') if isinstance(alert, dict) else '',
            'alert_tone': alert.get('tone', '') if isinstance(alert, dict) else '',
            'alert_message': alert.get('message', '') if isinstance(alert, dict) else '',
            'alert_date': alert.get('report_date', '') if isinstance(alert, dict) else '',
        }
    print(f"  去重後 {len(api_project_index)} 個唯一 project")

    # 所有已知的建照號碼
    all_permits = set(gov_data.keys())
    for permit in drive_names:
        all_permits.add(permit)

    print(f"\n{'=' * 60}")
    print(f"🔄 開始交叉比對 {len(all_permits)} 個建照...")
    print(f"{'=' * 60}\n")

    generic_name_pat = re.compile(r'^(監測報告?|監測報表?|監測$|安全觀測|安全監測|觀測報告|觀測數據|工地監測|工地$|報告$|報表$|告示牌|基地觀測系統|建號\d)')
    updated = 0
    for permit in sorted(all_permits):
        entry = registry.get(permit, {})
        changed = False
        # 每次重新比對時清除舊的 live_alerts（避免殘留錯誤匹配）
        if 'live_alerts' in entry:
            del entry['live_alerts']
            changed = True

        # 優先順序合併名稱（短名稱/通用名稱也可被更好的名稱覆蓋）
        current_name = entry.get('name', '')
        current_source = entry.get('name_source', '')
        is_poor_name = (
            not current_name or
            len(current_name) <= 3 or
            generic_name_pat.match(current_name) if current_name else True
        )
        # 手動確認和 alert_csv 不覆蓋
        is_protected = current_source in ('manual', 'alert_csv')

        if not is_protected:
            # 來源 3: Drive PDF 檔名（優先於 source_folder）
            if permit in drive_names:
                drive_name = drive_names[permit]['name']
                if is_poor_name or (current_source == 'source_folder' and len(drive_name) > len(current_name)):
                    entry['name'] = drive_name
                    entry['name_source'] = 'drive_pdf'
                    changed = True

            # 來源 2: 來源資料夾（補充用）
            if not entry.get('name') and permit in source_names:
                entry['name'] = source_names[permit]
                entry['name_source'] = 'source_folder'
                changed = True

        # 合併 Drive 資料
        if permit in drive_names:
            entry['pdf_count'] = drive_names[permit].get('pdf_count', 0)

        # 合併 API 資料（用檔名關鍵字匹配）
        if not entry.get('address') and permit in drive_names:
            drive_name = drive_names[permit]['name']
            matched_api = None

            for api_name, api_info in api_project_index.items():
                # 方法 1: 完整子字串匹配（4+ 字）
                if len(drive_name) >= 4 and drive_name in api_name:
                    matched_api = (api_name, api_info)
                    break
                api_clean = extract_name_from_text(api_name)
                if len(api_clean) >= 4 and api_clean in drive_name:
                    matched_api = (api_name, api_info)
                    break

            # 方法 2: 滑動視窗匹配（3+ 字，唯一匹配）
            if not matched_api:
                drive_cjk = re.sub(r'[^\u4e00-\u9fff]', '', re.sub(r'(安全觀測|監測報表?|觀測報告|觀測數據|報告|報表|數據)', '', drive_name))
                for flen in range(min(6, len(drive_cjk)), 2, -1):
                    found = False
                    for start in range(len(drive_cjk) - flen + 1):
                        sub = drive_cjk[start:start + flen]
                        matches = [(n, i) for n, i in api_project_index.items() if sub in n]
                        if len(matches) == 1:
                            matched_api = matches[0]
                            found = True
                            break
                    if found:
                        break

            if matched_api:
                api_name, api_info = matched_api
                entry['address'] = api_info['address']
                entry['stage'] = api_info['stage']
                entry['sensors'] = api_info['sensors']
                entry['alert_label'] = api_info['alert_label']
                entry['alert_tone'] = api_info['alert_tone']
                entry['alert_message'] = api_info['alert_message']
                entry['alert_date'] = api_info['alert_date']
                entry['api_match'] = api_name
                # API 名稱優先於 drive_pdf 和 source_folder（除非手動確認或 alert_csv）
                generic_name = re.compile(r'^(監測報告|監測報表|安全觀測報告書?|安全監測系統|觀測報告|觀測數據|工地監測數據)')
                should_replace = (
                    not entry.get('name') or
                    entry.get('name_source') in ('drive_pdf', 'source_folder') or
                    generic_name.match(entry.get('name', ''))
                )
                if should_replace:
                    api_clean = extract_name_from_text(api_name)
                    if api_clean:
                        entry['name'] = api_clean
                        entry['name_source'] = 'api_match'
                changed = True

        # 合併即時警示資料（用名稱匹配，含 api_match）
        # 通用名稱不可用於警示匹配（會造成誤配）
        generic_alert = re.compile(r'^(監測報告?|監測報表?|監測$|安全觀測報告書?|安全監測系統|觀測報告|觀測數據|工地監測數據)')
        if entry.get('name') or entry.get('api_match'):
            names_to_try = [n for n in [entry.get('api_match', ''), entry.get('name', '')] if n and not generic_alert.match(n)]
            for alert_project, alert_list in live_alerts.items():
                matched_alert = False
                for try_name in names_to_try:
                    if not try_name:
                        continue
                    # 完整子字串匹配
                    if (len(try_name) >= 3 and try_name in alert_project) or \
                       (len(alert_project) >= 3 and alert_project in try_name):
                        matched_alert = True
                        break
                    # 滑動視窗匹配（3+ 字，唯一匹配）
                    cjk = re.sub(r'[^\u4e00-\u9fff]', '', re.sub(r'(安全觀測|監測|觀測|報告|報表|數據|新建工程|工程)', '', try_name))
                    for flen in range(min(5, len(cjk)), 2, -1):
                        for start in range(len(cjk) - flen + 1):
                            sub = cjk[start:start + flen]
                            alert_matches = [ap for ap in live_alerts.keys() if sub in ap]
                            if len(alert_matches) == 1 and sub in alert_project:
                                matched_alert = True
                                break
                        if matched_alert:
                            break
                    if matched_alert:
                        break
                if matched_alert:
                    danger = sum(1 for a in alert_list if a['tone'] == 'danger')
                    warning = sum(1 for a in alert_list if a['tone'] == 'warning')
                    latest_date = max((a['date'] for a in alert_list if a['date']), default='')
                    entry['live_alerts'] = {
                        'danger': danger,
                        'warning': warning,
                        'total': len(alert_list),
                        'latest_date': latest_date,
                        'details': [a['detail'] for a in alert_list[:3]],
                    }
                    if not changed:
                        changed = True
                    break

        # 合併來源 URL
        if permit in gov_data:
            entry['source_url'] = gov_data[permit].get('source_url', '')

        if changed:
            updated += 1

        entry['updated_at'] = datetime.now().isoformat()
        registry[permit] = entry

    # 名稱優化：有 api_match 的建案，用 API 名稱取代通用/較差的名稱
    generic_name_pat = re.compile(r'^(監測報告?|監測報表?|監測$|安全觀測|安全監測|觀測報告|觀測數據|工地監測|工地$|報告$|報表$|告示牌|基地觀測系統|建號\d)')
    name_upgraded = 0
    for permit, entry in registry.items():
        api_match = entry.get('api_match', '')
        if not api_match:
            continue
        current_name = entry.get('name', '')
        current_source = entry.get('name_source', '')
        # 手動確認和 alert_csv 的名稱不覆蓋
        if current_source in ('manual', 'alert_csv'):
            continue
        # 以下情況用 API 名稱覆蓋
        should_upgrade = (
            not current_name or
            len(current_name) <= 3 or
            generic_name_pat.match(current_name) or
            current_source in ('drive_pdf', 'source_folder')
        )
        if should_upgrade:
            api_clean = extract_name_from_text(api_match)
            if api_clean and api_clean != current_name:
                entry['name'] = api_clean
                entry['name_source'] = 'api_match'
                name_upgraded += 1
    # 名稱清理：去除所有名稱中的壞模式（不論是否有 api_match）
    bad_suffix = re.compile(r'[-_\s]*(初始?值|_compressed|日報表).*$')
    bad_prefix = re.compile(r'^(P-\s*|\d+\.\s*)')
    name_cleaned = 0
    for permit, entry in registry.items():
        name = entry.get('name', '')
        if not name or entry.get('name_source') == 'manual':
            continue
        cleaned = bad_suffix.sub('', name)
        cleaned = bad_prefix.sub('', cleaned)
        cleaned = cleaned.strip(' -_')
        if cleaned != name and len(cleaned) >= 2:
            entry['name'] = cleaned
            name_cleaned += 1
        elif generic_name_pat.match(name) and not entry.get('api_match'):
            # 無法修的通用名稱 → 清空，讓報告顯示空白而不是垃圾
            entry['name'] = ''
            entry['name_source'] = ''
            name_cleaned += 1
    if name_cleaned:
        print(f"  名稱清理: {name_cleaned} 個建案名稱已修正或清空")

    if name_upgraded:
        print(f"  名稱優化: {name_upgraded} 個建案改用 API 名稱")

    # 統計
    has_name = sum(1 for e in registry.values() if e.get('name'))
    has_address = sum(1 for e in registry.values() if e.get('address'))
    has_stage = sum(1 for e in registry.values() if e.get('stage'))
    has_api = sum(1 for e in registry.values() if e.get('api_match'))

    print(f"\n{'=' * 60}")
    print(f"📊 比對結果")
    print(f"{'=' * 60}")
    print(f"  總建照數: {len(registry)}")
    print(f"  有名稱: {has_name} ({has_name * 100 // len(registry)}%)")
    print(f"  有地址: {has_address}")
    print(f"  有施工階段: {has_stage}")
    print(f"  有 API 匹配: {has_api}")
    print(f"  本次更新: {updated}")

    # 名稱來源分布
    source_counts = Counter(e.get('name_source', 'none') for e in registry.values())
    print(f"\n  名稱來源分布:")
    for src, count in source_counts.most_common():
        print(f"    {src}: {count}")

    # 儲存
    os.makedirs(os.path.dirname(REGISTRY_FILE), exist_ok=True)
    with open(REGISTRY_FILE, 'w', encoding='utf-8') as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
    print(f"\n✅ 已儲存到 {REGISTRY_FILE}")


if __name__ == '__main__':
    import argparse
    from city_config import get_cities_for_cli

    parser = argparse.ArgumentParser()
    parser.add_argument('--city', default=None, help='City ID or "all"')
    args = parser.parse_args()

    cities = get_cities_for_cli(args.city)
    for city in cities:
        build_registry(city=city)
