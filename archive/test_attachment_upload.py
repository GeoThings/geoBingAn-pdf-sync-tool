#!/usr/bin/env python3
"""
測試補充上傳工具
快速驗證 upload_attachments.py 是否能正常運作
"""
from config import JWT_TOKEN, GROUP_ID, USER_EMAIL
import requests
import subprocess

DJANGO_BASE_URL = 'http://localhost:8000'

print("🧪 測試補充上傳工具")
print("=" * 60)

# 測試 1: 檢查 Django API 連線
print("\n測試 1: 檢查 Django API 連線...")
try:
    response = requests.get(
        f'{DJANGO_BASE_URL}/health/',
        timeout=5
    )
    if response.status_code == 200:
        print("✅ Django API 正常")
    else:
        print(f"⚠️  Django API 回應異常: {response.status_code}")
except Exception as e:
    print(f"❌ 無法連線到 Django API: {e}")
    exit(1)

# 測試 2: 檢查 JWT 認證
print("\n測試 2: 檢查 JWT 認證...")
try:
    response = requests.get(
        f'{DJANGO_BASE_URL}/api/reports/reports/',
        headers={'Authorization': f'Bearer {JWT_TOKEN}'},
        params={'limit': 1},
        timeout=10
    )
    if response.status_code == 200:
        print(f"✅ JWT 認證成功（用戶: {USER_EMAIL}）")
    elif response.status_code == 401:
        print("❌ JWT 認證失敗：Token 無效或過期")
        exit(1)
    else:
        print(f"⚠️  API 回應異常: {response.status_code}")
except Exception as e:
    print(f"❌ 認證測試失敗: {e}")
    exit(1)

# 測試 3: 查詢沒有附件的 Reports
print("\n測試 3: 查詢沒有附件的 Reports...")
try:
    response = requests.get(
        f'{DJANGO_BASE_URL}/api/reports/reports/',
        headers={'Authorization': f'Bearer {JWT_TOKEN}'},
        params={
            'created_by__email': USER_EMAIL,
            'group': GROUP_ID,
            'limit': 100,
            'ordering': '-created_at'
        },
        timeout=10
    )

    if response.status_code != 200:
        print(f"❌ 查詢失敗: {response.status_code}")
        exit(1)

    data = response.json()
    all_reports = data.get('results', [])

    # 過濾沒有附件的
    reports_without_attachments = []
    for report in all_reports:
        attachments = report.get('attachments', [])
        metadata = report.get('metadata', {})
        if (not attachments or len(attachments) == 0) and metadata.get('source_file'):
            reports_without_attachments.append(report)

    print(f"✅ 找到 {len(reports_without_attachments)} 個沒有附件的 Reports")

    if reports_without_attachments:
        print(f"\n📋 範例 Reports:")
        for i, report in enumerate(reports_without_attachments[:3], 1):
            print(f"  {i}. {report.get('title', 'N/A')}")
            print(f"     ID: {report['id']}")
            print(f"     檔名: {report.get('metadata', {}).get('source_file', 'N/A')}")

except Exception as e:
    print(f"❌ 查詢失敗: {e}")
    exit(1)

# 測試 4: 檢查 Docker 容器
print("\n測試 4: 檢查 Docker 容器...")
try:
    result = subprocess.run(
        ['docker', 'ps', '--filter', 'name=geobingan-web', '--format', '{{.Names}}'],
        capture_output=True,
        text=True,
        timeout=5
    )

    if 'geobingan-web' in result.stdout:
        print("✅ Docker 容器 geobingan-web 正在運行")
    else:
        print("❌ Docker 容器 geobingan-web 未運行")
        print("   請執行: cd ../geoBingAn_v2_backend && ./scripts/local-deploy.sh start")
        exit(1)

except Exception as e:
    print(f"⚠️  無法檢查 Docker 容器: {e}")

# 測試 5: 測試 Django shell 存取
print("\n測試 5: 測試 Django shell 存取...")
try:
    result = subprocess.run(
        ['docker', 'exec', 'geobingan-web', 'python', 'manage.py', 'shell', '-c',
         'from apps.reports.models import Report; print("OK")'],
        capture_output=True,
        text=True,
        timeout=10
    )

    if 'OK' in result.stdout:
        print("✅ Django shell 可以執行")
    else:
        print(f"⚠️  Django shell 輸出異常: {result.stdout}")

except subprocess.TimeoutExpired:
    print("❌ Django shell 執行超時")
    exit(1)
except Exception as e:
    print(f"❌ Django shell 測試失敗: {e}")
    exit(1)

# 測試 6: 檢查 Google Drive API 金鑰
print("\n測試 6: 檢查 Google Drive API 金鑰...")
try:
    from pathlib import Path
    from google.oauth2 import service_account

    # 使用與 upload_pdfs.py 相同的金鑰路徑
    key_file = Path('/Users/geothingsmacbookair/Downloads/credentials.json')
    if not key_file.exists():
        print("❌ 找不到 credentials.json")
        print("   路徑: /Users/geothingsmacbookair/Downloads/credentials.json")
        exit(1)

    credentials = service_account.Credentials.from_service_account_file(
        str(key_file),
        scopes=['https://www.googleapis.com/auth/drive.readonly']
    )

    print("✅ Service Account 金鑰載入成功")

except Exception as e:
    print(f"❌ Service Account 金鑰載入失敗: {e}")
    exit(1)

# 所有測試通過
print("\n" + "=" * 60)
print("✅ 所有測試通過")
print("=" * 60)
print("\n📝 下一步:")
print("  1. 執行補充上傳工具: python3 upload_attachments.py")
print("  2. 或執行完整流程: ./upload_complete.sh")
print("")
