#!/usr/bin/env python3
"""
單筆 PDF 上傳測試
直接上傳一個指定的 PDF 到 geoBingAn Backend API
"""
import io
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# 匯入配置
from config import JWT_TOKEN, USER_EMAIL, GROUP_ID, GEOBINGAN_API_URL

# Google Drive 設定
SERVICE_ACCOUNT_FILE = '/Users/geothingsmacbookair/Downloads/credentials.json'
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
SHARED_DRIVE_ID = '0AIvp1h-6BZ1oUk9PVA'

# API 設定
GEOBINGAN_SCENARIO_ID = 'construction_safety_pdf'
GEOBINGAN_LANGUAGE = 'zh-TW'

print("=" * 60)
print("🧪 單筆 PDF 上傳測試")
print("=" * 60)

# 初始化 Google Drive API
print("\n🔑 初始化 Google Drive API...")
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)
service = build('drive', 'v3', credentials=credentials)
print(f"✅ 已初始化 ({credentials.service_account_email})")

# 搜尋最近的一個 PDF
print("\n🔍 搜尋最近更新的 PDF...")
query = (
    f"mimeType = 'application/pdf' and "
    f"trashed = false"
)

results = service.files().list(
    q=query,
    pageSize=5,
    includeItemsFromAllDrives=True,
    supportsAllDrives=True,
    corpora='drive',
    driveId=SHARED_DRIVE_ID,
    fields='files(id, name, size, modifiedTime, parents)',
    orderBy='modifiedTime desc'
).execute()

files = results.get('files', [])

if not files:
    print("❌ 找不到 PDF 檔案")
    exit(1)

pdf = files[0]
print(f"\n✅ 找到 PDF:")
print(f"  檔名: {pdf['name']}")
print(f"  大小: {int(pdf.get('size', 0)) / 1024 / 1024:.2f} MB")
print(f"  更新時間: {pdf['modifiedTime']}")

# 取得父資料夾名稱（建案代碼）
folder_id = pdf['parents'][0]
folder = service.files().get(
    fileId=folder_id,
    supportsAllDrives=True,
    fields='name'
).execute()
folder_name = folder['name']
print(f"  建案代碼: {folder_name}")

# 下載 PDF
print(f"\n📥 下載 PDF...")
request = service.files().get_media(fileId=pdf['id'])
fh = io.BytesIO()
downloader = MediaIoBaseDownload(fh, request)

done = False
while not done:
    status, done = downloader.next_chunk()
    if status:
        print(f"  下載進度: {int(status.progress() * 100)}%", end='\r')

print(f"\n✅ 下載完成")

# 上傳到 geoBingAn Backend
print(f"\n📤 上傳到 geoBingAn Backend...")
print(f"  API: {GEOBINGAN_API_URL}")
print(f"  User: {USER_EMAIL}")

fh.seek(0)
files_data = {
    'file': (pdf['name'], fh, 'application/pdf')
}

data = {
    'scenario_id': GEOBINGAN_SCENARIO_ID,
    'language': GEOBINGAN_LANGUAGE,
    'save_to_report': 'true',
    'additional_context': f"建案代碼: {folder_name}",
    'group_id': GROUP_ID
}

headers = {
    'Authorization': f'Bearer {JWT_TOKEN}'
}

try:
    response = requests.post(
        GEOBINGAN_API_URL,
        files=files_data,
        data=data,
        headers=headers,
        timeout=300
    )

    print(f"\n📊 API 回應:")
    print(f"  狀態碼: {response.status_code}")

    if response.status_code == 200 or response.status_code == 201:
        result = response.json()
        print(f"\n✅ 上傳成功!")
        print(f"  Report ID: {result.get('report_id', 'N/A')}")

        if 'construction_project' in result:
            project = result['construction_project']
            print(f"  專案代碼: {project.get('project_code', 'N/A')}")
            print(f"  監測報告 ID: {project.get('monitoring_report_id', 'N/A')}")
    else:
        print(f"\n❌ 上傳失敗")
        print(f"  錯誤訊息: {response.text[:500]}")

except Exception as e:
    print(f"\n❌ 發生錯誤: {e}")

print("\n" + "=" * 60)
print("測試完成")
print("=" * 60)
