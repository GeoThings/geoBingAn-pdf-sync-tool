#!/usr/bin/env python3
"""
查詢最近上傳的 PDF 和 AI 分析狀態
"""
import subprocess
import json
from datetime import datetime, timedelta

def check_upload_status():
    """檢查上傳狀態"""

    print("=" * 80)
    print("📊 上傳狀態檢查工具")
    print("=" * 80)

    # 1. 檢查狀態檔案
    print("\n1️⃣  狀態檔案檢查:")
    print("-" * 80)
    try:
        with open('state/uploaded_to_geobingan_7days.json', 'r', encoding='utf-8') as f:
            state = json.load(f)
            print(f"✅ 已上傳檔案數: {len(state['uploaded_files'])}")
            print(f"❌ 錯誤記錄數: {len(state['errors'])}")

            if state['uploaded_files']:
                print("\n最近上傳的檔案:")
                for i, file in enumerate(state['uploaded_files'][-5:], 1):  # 只顯示最後 5 個
                    print(f"  {i}. {file}")

            if state['errors']:
                print("\n錯誤記錄:")
                for error in state['errors']:
                    print(f"  ❌ {error}")
    except FileNotFoundError:
        print("⚠️  找不到狀態檔案")

    # 2. 查詢資料庫 Reports
    print("\n2️⃣  資料庫 Reports 檢查:")
    print("-" * 80)

    django_command = """
from apps.reports.models import Report
from django.utils import timezone
from datetime import timedelta
import json

recent_time = timezone.now() - timedelta(hours=2)
reports = Report.objects.filter(created_at__gte=recent_time).order_by('-created_at')

print(f"找到 {reports.count()} 個 Reports")
print()

for i, report in enumerate(reports, 1):
    print(f"{i}. {report.title}")
    print(f"   ID: {report.id}")
    print(f"   來源: {report.metadata.get('source_file') if report.metadata else '無'}")
    print(f"   AI 模型: {report.metadata.get('analysis_model') if report.metadata else '無'}")
    print(f"   信心分數: {report.metadata.get('confidence_score') if report.metadata else '無'}")
    print(f"   附件: {report.attachments.count()} 個")

    # 檢查是否有 AI 分析內容
    if report.content:
        try:
            content = json.loads(report.content) if isinstance(report.content, str) else report.content
            has_analysis = bool(content)
            print(f"   ✅ AI 分析: {'完成' if has_analysis else '未完成'} ({len(str(content))} 字元)")
        except:
            print(f"   ⚠️  AI 分析: 格式異常")
    else:
        print(f"   ❌ AI 分析: 無")
    print()
"""

    result = subprocess.run(
        ['docker', 'exec', 'geobingan-web', 'python', 'manage.py', 'shell', '-c', django_command],
        capture_output=True,
        text=True,
        timeout=30
    )

    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f"❌ 查詢失敗: {result.stderr}")

    # 3. 統計摘要
    print("\n3️⃣  統計摘要:")
    print("-" * 80)

    summary_command = """
from apps.reports.models import Report, FileAttachment
from django.utils import timezone
from datetime import timedelta

recent_time = timezone.now() - timedelta(hours=2)
reports = Report.objects.filter(created_at__gte=recent_time)
attachments = FileAttachment.objects.filter(uploaded_at__gte=recent_time)

print(f"最近 2 小時:")
print(f"  Reports 建立: {reports.count()} 個")
print(f"  FileAttachments 建立: {attachments.count()} 個")
print(f"  有附件的 Reports: {reports.filter(attachments__isnull=False).distinct().count()} 個")
print(f"  有 AI 分析的 Reports: {reports.exclude(content='').exclude(content__isnull=True).count()} 個")
"""

    result = subprocess.run(
        ['docker', 'exec', 'geobingan-web', 'python', 'manage.py', 'shell', '-c', summary_command],
        capture_output=True,
        text=True,
        timeout=30
    )

    if result.returncode == 0:
        print(result.stdout)
    else:
        print(f"❌ 查詢失敗: {result.stderr}")

    print("\n" + "=" * 80)
    print("✅ 狀態檢查完成")
    print("=" * 80)

if __name__ == '__main__':
    check_upload_status()
