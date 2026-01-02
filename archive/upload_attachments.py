#!/usr/bin/env python3
"""
補充上傳 PDF 附件到 S3
用於為已建立的 Report 補上缺失的 PDF 附件

執行方式：
    python3 upload_attachments.py

功能：
1. 從 Django API 取得沒有附件的 Reports
2. 從 Google Drive 重新下載原始 PDF
3. 使用 Django shell 建立 FileAttachment 並上傳到 S3
"""
from config import JWT_TOKEN, GROUP_ID, GEOBINGAN_API_URL, USER_EMAIL
import requests
import json
import base64
import subprocess
import sys
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

# Google Drive API 設定（與 upload_pdfs.py 使用相同的金鑰）
SERVICE_ACCOUNT_FILE = '/Users/geothingsmacbookair/Downloads/credentials.json'
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
SHARED_DRIVE_ID = '0AIvp1h-6BZ1oUk9PVA'  # 與 upload_pdfs.py 相同

# Django API 基礎 URL
DJANGO_BASE_URL = 'http://localhost:8000'

print(f"🔧 補充上傳 PDF 附件工具")
print(f"=" * 60)
print(f"用戶: {USER_EMAIL}")
print(f"群組 ID: {GROUP_ID}")
print(f"=" * 60)


def get_drive_service():
    """初始化 Google Drive API 服務"""
    try:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
        service = build('drive', 'v3', credentials=credentials)

        # 測試連線
        about = service.about().get(fields='user').execute()
        print(f"✅ Google Drive API 已初始化 ({about['user']['emailAddress']})")
        return service
    except Exception as e:
        print(f"❌ 無法初始化 Google Drive API: {e}")
        sys.exit(1)


def get_reports_without_attachments():
    """使用 Django shell 直接查詢沒有附件的 Reports"""
    print(f"\n📋 正在查詢沒有附件的 Reports...")

    try:
        # 使用 Django shell 直接查詢
        django_command = f"""
from apps.reports.models import Report
from django.contrib.auth import get_user_model
import json

User = get_user_model()
user = User.objects.get(email='{USER_EMAIL}')

# 查詢沒有附件的 Reports
reports = Report.objects.filter(
    created_by=user,
    group_id='{GROUP_ID}'
).prefetch_related('attachments').order_by('-created_at')[:100]

# 過濾出沒有附件且有 source_file 的
results = []
for report in reports:
    if report.attachments.count() == 0 and report.metadata and report.metadata.get('source_file'):
        results.append({{
            'id': str(report.id),
            'title': report.title,
            'created_at': report.created_at.isoformat(),
            'metadata': report.metadata
        }})

# 輸出 JSON（使用特殊標記）
print('JSON_START')
print(json.dumps(results, ensure_ascii=False))
print('JSON_END')
"""

        result = subprocess.run(
            ['docker', 'exec', 'geobingan-web', 'python', 'manage.py', 'shell', '-c', django_command],
            capture_output=True,
            text=True,
            timeout=30
        )

        # 解析輸出
        output = result.stdout

        # 提取 JSON 部分
        if 'JSON_START' in output and 'JSON_END' in output:
            json_start = output.index('JSON_START') + len('JSON_START')
            json_end = output.index('JSON_END')
            json_str = output[json_start:json_end].strip()

            reports = json.loads(json_str)
            print(f"✅ 找到 {len(reports)} 個沒有附件的 Reports")
            return reports
        else:
            print(f"❌ 無法解析 Django 回應")
            if result.stderr:
                print(f"   stderr: {result.stderr[:500]}")
            return []

    except subprocess.TimeoutExpired:
        print(f"❌ Django shell 執行超時")
        return []
    except Exception as e:
        print(f"❌ 查詢 Reports 失敗: {e}")
        import traceback
        traceback.print_exc()
        return []


def search_file_in_drive(drive_service, file_name):
    """在 Google Drive（包含 Shared Drive）中搜尋檔案"""
    try:
        # 嘗試多種搜尋策略
        search_strategies = [
            # 策略 1: 完整檔名匹配（Shared Drive）
            {
                'query': f"name='{file_name}' and mimeType='application/pdf' and trashed=false",
                'drive_id': SHARED_DRIVE_ID,
                'include_items_from_all_drives': True,
                'supports_all_drives': True,
                'corpora': 'drive'
            },
            # 策略 2: 檔名包含（部分匹配，Shared Drive）
            {
                'query': f"name contains '{file_name.replace('.pdf', '')}' and mimeType='application/pdf' and trashed=false",
                'drive_id': SHARED_DRIVE_ID,
                'include_items_from_all_drives': True,
                'supports_all_drives': True,
                'corpora': 'drive'
            },
            # 策略 3: 我的雲端硬碟
            {
                'query': f"name='{file_name}' and mimeType='application/pdf' and trashed=false",
                'corpora': 'user'
            },
        ]

        for i, strategy in enumerate(search_strategies, 1):
            results = drive_service.files().list(
                q=strategy['query'],
                fields='files(id, name, mimeType, size, modifiedTime)',
                pageSize=10,
                driveId=strategy.get('drive_id'),
                includeItemsFromAllDrives=strategy.get('include_items_from_all_drives', False),
                supportsAllDrives=strategy.get('supports_all_drives', False),
                corpora=strategy.get('corpora', 'user')
            ).execute()

            files = results.get('files', [])

            if files:
                if len(files) > 1:
                    print(f"  ℹ️  找到 {len(files)} 個匹配檔案（策略 {i}），使用最新的")
                    # 使用最新修改的檔案
                    files.sort(key=lambda x: x['modifiedTime'], reverse=True)
                else:
                    print(f"  ✅ 找到檔案（策略 {i}）")

                return files[0]

        # 所有策略都失敗
        print(f"  ⚠️  找不到檔案: {file_name}")
        return None

    except Exception as e:
        print(f"  ❌ 搜尋檔案失敗: {e}")
        import traceback
        traceback.print_exc()
        return None


def download_file_from_drive(drive_service, file_id):
    """從 Google Drive 下載檔案內容"""
    try:
        request = drive_service.files().get_media(fileId=file_id)
        file_content = io.BytesIO()
        downloader = MediaIoBaseDownload(file_content, request)

        done = False
        while not done:
            status, done = downloader.next_chunk()

        file_content.seek(0)
        return file_content.read()

    except Exception as e:
        print(f"  ❌ 下載檔案失敗: {e}")
        return None


def create_attachment_via_django(report_id, file_name, file_content):
    """使用 Django shell 建立 FileAttachment 並上傳到 S3（使用臨時檔案）"""
    import tempfile
    import os

    temp_file_path = None
    container_file_path = None

    try:
        # 1. 建立臨時檔案
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.pdf', delete=False) as temp_file:
            temp_file.write(file_content)
            temp_file_path = temp_file.name

        # 2. 複製檔案到 Docker 容器
        container_file_path = f'/tmp/upload_{report_id}.pdf'
        subprocess.run(
            ['docker', 'cp', temp_file_path, f'geobingan-web:{container_file_path}'],
            check=True,
            timeout=30
        )

        # 3. 在 Django shell 中讀取檔案並建立附件
        django_command = f"""
from apps.reports.models import Report
from apps.reports.services.attachment_service import AttachmentService
from django.core.files.uploadedfile import SimpleUploadedFile
import os

try:
    # 取得 Report
    report = Report.objects.get(id='{report_id}')

    # 讀取檔案內容
    with open('{container_file_path}', 'rb') as f:
        file_content = f.read()

    # 建立 UploadedFile 物件
    uploaded_file = SimpleUploadedFile(
        name='{file_name}',
        content=file_content,
        content_type='application/pdf'
    )

    # 使用 AttachmentService 建立附件（會自動上傳到 S3）
    attachment = AttachmentService.handle_uploaded_file(report, uploaded_file)

    # 刪除臨時檔案
    os.remove('{container_file_path}')

    print(f'SUCCESS:{{attachment.id}}:{{attachment.file_path}}')

except Exception as e:
    import traceback
    print(f'ERROR:{{str(e)}}')
    traceback.print_exc()
    # 清理臨時檔案
    try:
        os.remove('{container_file_path}')
    except:
        pass
"""

        # 4. 執行 Django shell 命令
        result = subprocess.run(
            ['docker', 'exec', 'geobingan-web', 'python', 'manage.py', 'shell', '-c', django_command],
            capture_output=True,
            text=True,
            timeout=60
        )

        # 5. 解析輸出
        output = result.stdout.strip()

        if 'SUCCESS:' in output:
            # 提取附件 ID 和路徑
            parts = output.split('SUCCESS:')[1].split(':')
            attachment_id = parts[0]
            file_path = parts[1] if len(parts) > 1 else 'N/A'

            print(f"  ✅ 附件已建立")
            print(f"     - Attachment ID: {attachment_id}")
            print(f"     - File Path: {file_path}")
            return True

        elif 'ERROR:' in output:
            error_msg = output.split('ERROR:')[1]
            print(f"  ❌ Django 錯誤: {error_msg}")
            if result.stderr:
                print(f"  stderr: {result.stderr[:500]}")
            return False

        else:
            print(f"  ❌ 未預期的輸出: {output[:200]}")
            if result.stderr:
                print(f"  stderr: {result.stderr[:500]}")
            return False

    except subprocess.TimeoutExpired:
        print(f"  ⏱️  Django shell 執行超時")
        return False

    except Exception as e:
        print(f"  ❌ 建立附件失敗: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        # 清理本地臨時檔案
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except:
                pass


def main():
    """主程式"""
    # 1. 初始化 Google Drive API
    drive_service = get_drive_service()

    # 2. 取得沒有附件的 Reports
    reports = get_reports_without_attachments()

    if not reports:
        print(f"\n✅ 沒有需要補充附件的 Reports")
        return

    # 3. 處理每個 Report
    print(f"\n{'=' * 60}")
    print(f"開始處理 {len(reports)} 個 Reports")
    print(f"{'=' * 60}\n")

    success_count = 0
    fail_count = 0
    skip_count = 0

    for i, report in enumerate(reports, 1):
        report_id = report['id']
        title = report.get('title', 'N/A')
        metadata = report.get('metadata', {})
        file_name = metadata.get('source_file', 'unknown.pdf')

        print(f"[{i}/{len(reports)}] 處理: {title}")
        print(f"  - Report ID: {report_id}")
        print(f"  - 檔名: {file_name}")

        # 3.1 搜尋檔案
        file_info = search_file_in_drive(drive_service, file_name)
        if not file_info:
            print(f"  ⏭️  跳過（找不到檔案）")
            skip_count += 1
            continue

        print(f"  - Google Drive ID: {file_info['id']}")
        print(f"  - 檔案大小: {int(file_info['size']) / 1024 / 1024:.2f} MB")

        # 3.2 下載檔案
        print(f"  📥 正在下載...")
        file_content = download_file_from_drive(drive_service, file_info['id'])
        if not file_content:
            print(f"  ⏭️  跳過（下載失敗）")
            fail_count += 1
            continue

        # 3.3 建立附件
        print(f"  📤 正在建立附件並上傳到 S3...")
        if create_attachment_via_django(report_id, file_name, file_content):
            success_count += 1
        else:
            fail_count += 1

        print()  # 空行分隔

    # 4. 輸出統計
    print(f"\n{'=' * 60}")
    print(f"處理完成")
    print(f"{'=' * 60}")
    print(f"✅ 成功: {success_count}")
    print(f"❌ 失敗: {fail_count}")
    print(f"⏭️  跳過: {skip_count}")
    print(f"📊 總計: {len(reports)}")
    print(f"{'=' * 60}\n")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n⚠️  使用者中斷")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ 未預期的錯誤: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
