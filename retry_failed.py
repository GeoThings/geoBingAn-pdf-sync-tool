#!/usr/bin/env python3
"""
重新上傳 4 個失敗的 PDF 檔案

這些檔案在之前的批次上傳中因為 float 錯誤而失敗。
現在程式碼已修復，可以重新上傳。
"""
import json
import sys
import io
import time
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ================== 設定區域 ==================
# Google Drive 認證
SERVICE_ACCOUNT_FILE = '/Users/geothingsmacbookair/Downloads/credentials.json'
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
SHARED_DRIVE_ID = '0AIvp1h-6BZ1oUk9PVA'

# geoBingAn API 設定
GEOBINGAN_API_URL = 'http://localhost:8000/api/reports/upload-file/'
GEOBINGAN_SCENARIO_ID = 'construction_safety_pdf'
GEOBINGAN_LANGUAGE = 'zh-TW'

# 狀態追蹤檔案
STATE_FILE = './state/uploaded_to_geobingan_7days.json'

# 失敗的檔案列表 (從 state file 讀取)
FAILED_FILES = [
    {
        "folder": "捷運監測報告",
        "file": "宏林TOP31-捷運報告-1141217.pdf",
        "file_id": "1KJS24iADaCrbrt8_f3LLXUbfpayYh0Fn"
    },
    {
        "folder": "113建字第0008號",
        "file": "長春段-捷運暨基地監測39週報告 (1141214-1220)更正.pdf",
        "file_id": "19PAJwIdKYOf41MnlxFN72nAd56cizsuf"
    },
    {
        "folder": "113建字第0182號",
        "file": "長見振家雙城街1141224.pdf",
        "file_id": "1tZjHCpf-Fpht40Rdmz68XPqGoRmL-x_H"
    },
    {
        "folder": "11412",
        "file": "1141225(瓶蓋工廠).pdf",
        "file_id": "1K229iBOoNZJivagrYeTnRG411d2pdHct"
    }
]
# ============================================


def get_drive_service():
    """初始化 Google Drive API"""
    print("🔑 初始化 Google Drive API")
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service = build('drive', 'v3', credentials=credentials)
    print(f"✅ 已初始化")
    return service


def download_pdf(service, file_id: str, file_name: str):
    """從 Google Drive 下載 PDF"""
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
    except Exception as e:
        print(f"  ❌ 下載失敗: {file_name} - {e}")
        return None


def upload_to_geobingan(pdf_content: bytes, file_name: str, project_code: str):
    """上傳 PDF 到 geoBingAn API"""
    try:
        files = {
            'file': (file_name, pdf_content, 'application/pdf')
        }

        data = {
            'scenario_id': GEOBINGAN_SCENARIO_ID,
            'language': GEOBINGAN_LANGUAGE,
            'save_to_report': True,
            'additional_context': f'建案代碼: {project_code}'
        }

        response = requests.post(
            GEOBINGAN_API_URL,
            files=files,
            data=data,
            timeout=300
        )

        if response.status_code == 200:
            result = response.json()
            report_id = result.get('report_id', 'N/A')
            construction_project = result.get('construction_project')

            if construction_project:
                print(f"  ✅ 分析成功")
                print(f"     - Report ID: {report_id}")
                print(f"     - 建案代碼: {construction_project.get('project_code')}")
                print(f"     - 監測報告ID: {construction_project.get('monitoring_report_id')}")
                return True
            elif result.get('success'):
                print(f"  ✅ 分析成功，Report ID: {report_id}")
                return True
            else:
                print(f"  ❌ 分析失敗: {result.get('error', 'Unknown error')}")
                return False
        else:
            print(f"  ❌ API 錯誤 ({response.status_code}): {response.text[:200]}")
            return False

    except Exception as e:
        print(f"  ❌ 上傳失敗: {e}")
        return False


def load_state():
    """載入狀態檔案"""
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {'uploaded_files': [], 'errors': []}


def save_state(state):
    """儲存狀態檔案"""
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, indent=2, ensure_ascii=False, fp=f)


def main():
    """主程式"""
    print("\n" + "=" * 60)
    print("🔄 重新上傳 4 個失敗的 PDF 檔案")
    print("=" * 60)

    # 初始化
    service = get_drive_service()
    state = load_state()

    # 統計
    success_count = 0
    fail_count = 0

    # 處理每個失敗的檔案
    for i, failed_file in enumerate(FAILED_FILES, 1):
        folder = failed_file['folder']
        file_name = failed_file['file']
        file_id = failed_file['file_id']
        project_code = folder

        print(f"\n[{i}/{len(FAILED_FILES)}] 處理: {folder}/{file_name}")

        # 下載 PDF
        pdf_content = download_pdf(service, file_id, file_name)
        if not pdf_content:
            fail_count += 1
            continue

        # 上傳到 geoBingAn
        if upload_to_geobingan(pdf_content, file_name, project_code):
            success_count += 1

            # 更新狀態：移除錯誤，加入成功列表
            file_path = f"{folder}/{file_name}"

            # 從 errors 列表中移除
            state['errors'] = [
                err for err in state['errors']
                if not (err['folder'] == folder and err['file'] == file_name)
            ]

            # 加入 uploaded_files（如果不存在）
            if file_path not in state['uploaded_files']:
                state['uploaded_files'].append(file_path)

            save_state(state)
        else:
            fail_count += 1

        # 延遲以避免 rate limit
        if i < len(FAILED_FILES):
            print(f"  ⏳ 等待 20 秒...")
            time.sleep(20)

    # 最終報告
    print("\n" + "=" * 60)
    print("📊 重新上傳完成統計")
    print("=" * 60)
    print(f"✅ 成功: {success_count} 個檔案")
    print(f"❌ 失敗: {fail_count} 個檔案")

    if fail_count > 0:
        print(f"\n仍有 {fail_count} 個檔案失敗，請檢查錯誤訊息")
    else:
        print(f"\n🎉 所有檔案都已成功上傳！")

    print("=" * 60)


if __name__ == '__main__':
    main()
