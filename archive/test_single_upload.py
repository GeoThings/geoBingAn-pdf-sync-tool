#!/usr/bin/env python3
"""
測試單一 PDF 上傳
快速測試 JWT 認證和上傳功能
"""
from config import JWT_TOKEN, GROUP_ID, GEOBINGAN_API_URL, USER_EMAIL
import requests
import sys

print(f"🧪 測試 PDF 上傳到 geoBingAn")
print(f"=" * 60)
print(f"用戶: {USER_EMAIL}")
print(f"群組 ID: {GROUP_ID}")
print(f"API URL: {GEOBINGAN_API_URL}")
print(f"=" * 60)

# 建立一個簡單的測試 PDF 內容
# 實際上我們使用一個很小的 PDF 結構
test_pdf_content = b"""%PDF-1.4
1 0 obj
<<
/Type /Catalog
/Pages 2 0 R
>>
endobj
2 0 obj
<<
/Type /Pages
/Kids [3 0 R]
/Count 1
>>
endobj
3 0 obj
<<
/Type /Page
/Parent 2 0 R
/MediaBox [0 0 612 792]
/Contents 4 0 R
>>
endobj
4 0 obj
<<
/Length 44
>>
stream
BT
/F1 12 Tf
100 700 Td
(Test PDF) Tj
ET
endstream
endobj
xref
0 5
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000214 00000 n
trailer
<<
/Size 5
/Root 1 0 R
>>
startxref
314
%%EOF
"""

# 準備上傳請求
files = {
    'file': ('test_construction_safety_report.pdf', test_pdf_content, 'application/pdf')
}

data = {
    'scenario_id': 'construction_safety_pdf',
    'language': 'zh-TW',
    'save_to_report': True,
    'group_id': GROUP_ID,
    'additional_context': '測試上傳 - JWT 認證測試'
}

headers = {
    'Authorization': f'Bearer {JWT_TOKEN}'
}

print(f"\n📤 正在上傳測試 PDF...")
print(f"  - 檔案名稱: test_construction_safety_report.pdf")
print(f"  - 檔案大小: {len(test_pdf_content)} bytes")
print(f"  - Scenario: construction_safety_pdf")
print(f"  - 認證: JWT Bearer Token")

try:
    response = requests.post(
        GEOBINGAN_API_URL,
        files=files,
        data=data,
        headers=headers,
        timeout=120
    )

    print(f"\n📊 API 回應:")
    print(f"  - HTTP Status: {response.status_code}")

    if response.status_code == 200:
        result = response.json()
        print(f"\n✅ 上傳成功！")
        print(f"\n📋 回應內容:")
        print(f"  - Success: {result.get('success', False)}")
        print(f"  - Report ID: {result.get('report_id', 'N/A')}")

        # 檢查是否有建立建案
        if 'construction_project' in result:
            project = result['construction_project']
            print(f"\n🏗️  建案資訊:")
            print(f"  - 建案代碼: {project.get('project_code', 'N/A')}")
            print(f"  - 建案名稱: {project.get('project_name', 'N/A')}")
            print(f"  - 監測報告 ID: {project.get('monitoring_report_id', 'N/A')}")

        # 檢查 AI 分析結果
        if 'content' in result:
            content = result['content']
            print(f"\n🤖 AI 分析摘要:")
            if 'analysis' in content:
                analysis = content['analysis']
                print(f"  - Safety Status: {analysis.get('safety_status', 'N/A')}")
                print(f"  - Confidence: {analysis.get('confidence', 'N/A')}")
                print(f"  - Summary: {analysis.get('summary', 'N/A')[:100]}...")

        print(f"\n" + "=" * 60)
        print(f"✅ 測試完成！請前往 Django Admin 查看報告")
        print(f"   URL: http://localhost:8000/admin/reports/report/")
        print(f"=" * 60)

        sys.exit(0)

    elif response.status_code == 401:
        print(f"\n❌ 認證失敗 (401 Unauthorized)")
        print(f"  - 錯誤: JWT Token 可能無效或過期")
        print(f"  - 回應: {response.text[:500]}")
        sys.exit(1)

    elif response.status_code == 403:
        print(f"\n❌ 權限不足 (403 Forbidden)")
        print(f"  - 錯誤: 用戶可能沒有權限存取該群組")
        print(f"  - 回應: {response.text[:500]}")
        sys.exit(1)

    elif response.status_code == 400:
        print(f"\n❌ 請求錯誤 (400 Bad Request)")
        print(f"  - 回應: {response.text[:500]}")
        sys.exit(1)

    else:
        print(f"\n❌ 上傳失敗")
        print(f"  - HTTP Status: {response.status_code}")
        print(f"  - 回應: {response.text[:500]}")
        sys.exit(1)

except requests.exceptions.Timeout:
    print(f"\n⏱️  請求超時（120 秒）")
    print(f"  - API 可能正在處理大型檔案")
    print(f"  - 建議: 檢查 Docker 容器狀態")
    sys.exit(1)

except requests.exceptions.ConnectionError as e:
    print(f"\n❌ 連線錯誤")
    print(f"  - 錯誤: {e}")
    print(f"  - 建議: 檢查 Docker 容器是否正在運行")
    print(f"  - 指令: docker-compose ps")
    sys.exit(1)

except Exception as e:
    print(f"\n❌ 未預期的錯誤")
    print(f"  - 錯誤: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
