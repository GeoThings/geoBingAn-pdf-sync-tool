# 測試上傳流程

## ✅ 配置完成

### 1. 用戶已建立
```
Email: jerryjo0802@gmail.com
User ID: a3b1da69-6640-4aba-9267-2ccc2f5b9186
密碼: TestPassword123!
```

### 2. 群組已建立
```
群組名稱: 測試上傳群組
群組 ID: 921630a9-41d6-436e-8da4-492f01446bdc
用戶角色: Owner（擁有者）
```

### 3. JWT Token 已生成
```
有效期: 365 天（至 2026-12-31）
權限: 完整用戶權限
長度: 215 字元
```

### 4. 配置檔案已建立
- ✅ `config.py` - 包含實際認證資訊
- ✅ `config.py.example` - 範例檔案
- ✅ `.gitignore` - 已加入 config.py

## 🧪 測試步驟

### 步驟 1: 驗證配置

```bash
cd /Users/geothingsmacbookair/Documents/GitHub/geoBingAn-pdf-sync-tool
python3 -c "from config import *; print('✅ 配置正常')"
```

### 步驟 2: 測試單一 PDF 上傳

建立一個測試腳本：

```python
# test_single_upload.py
from config import JWT_TOKEN, GROUP_ID, GEOBINGAN_API_URL
import requests

# 準備測試 PDF（使用簡單的文字檔案模擬）
test_content = b'%PDF-1.4\nTest PDF Content for geoBingAn'

files = {
    'file': ('test_construction_report.pdf', test_content, 'application/pdf')
}

data = {
    'scenario_id': 'construction_safety_pdf',
    'language': 'zh-TW',
    'save_to_report': True,
    'group_id': GROUP_ID,
    'additional_context': '測試上傳 - 手動認證'
}

headers = {
    'Authorization': f'Bearer {JWT_TOKEN}'
}

print('🧪 測試上傳到 geoBingAn...')
print(f'  - API URL: {GEOBINGAN_API_URL}')
print(f'  - Group ID: {GROUP_ID}')
print(f'  - 認證: JWT Bearer Token')

response = requests.post(
    GEOBINGAN_API_URL,
    files=files,
    data=data,
    headers=headers,
    timeout=60
)

print(f'\n📊 回應狀態: {response.status_code}')
if response.status_code == 200:
    result = response.json()
    print('✅ 上傳成功')
    print(f'  - Report ID: {result.get("report_id", "N/A")}')
    print(f'  - Success: {result.get("success")}')
    if 'construction_project' in result:
        print(f'  - 建案代碼: {result["construction_project"].get("project_code")}')
else:
    print('❌ 上傳失敗')
    print(f'  - 錯誤: {response.text[:500]}')
```

執行測試：
```bash
python3 test_single_upload.py
```

### 步驟 3: 執行完整上傳

```bash
python3 upload_pdfs.py
```

## 📋 預期結果

### 成功上傳的標誌：

1. **控制台輸出**:
```
✅ 已載入認證配置（用戶: jerryjo0802@gmail.com）
🔑 初始化 Google Drive API (Service Account)
...
✅ 分析成功，Report ID: {uuid}
✅ 建案建立成功
```

2. **Django Admin 檢查**:
- 前往 http://localhost:8000/admin/
- 登入 admin@geobingan.com / admin123456
- 查看 Reports → Reports
- 篩選 `created_by = jerryjo0802@gmail.com`

3. **資料庫驗證**:
```bash
docker exec geobingan-web python manage.py shell -c "
from apps.reports.models import Report
from django.contrib.auth import get_user_model

User = get_user_model()
user = User.objects.get(email='jerryjo0802@gmail.com')

reports = Report.objects.filter(created_by=user)
print(f'✅ 用戶上傳的報告數量: {reports.count()}')
for report in reports[:5]:
    print(f'  - {report.title} (ID: {report.id})')
"
```

## 🔔 通知測試

如果 PDF 分析結果為 **action** 或 **critical**:

1. **檢查通知記錄**:
```bash
docker exec geobingan-web python manage.py shell -c "
from apps.notifications.models import NotificationQueue

notifications = NotificationQueue.objects.all().order_by('-created_at')[:10]
print(f'最近 10 筆通知:')
for notif in notifications:
    print(f'  - {notif.subject} → {notif.recipient_email}')
"
```

2. **檢查 Celery 日誌**:
```bash
docker-compose logs celery | grep "danger_alert"
```

## ❌ 故障排除

### 問題 1: 401 Unauthorized

**檢查**:
```bash
# 驗證 JWT Token
docker exec geobingan-web python manage.py shell -c "
from django.conf import settings
import jwt

token = 'your-token-here'
try:
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=['HS256'])
    print('✅ Token 有效')
    print(f'  - User ID: {payload[\"user_id\"]}')
    print(f'  - Type: {payload[\"type\"]}')
except Exception as e:
    print(f'❌ Token 無效: {e}')
"
```

### 問題 2: 403 Forbidden

**檢查群組成員資格**:
```bash
docker exec geobingan-web python manage.py shell -c "
from django.contrib.auth import get_user_model
from apps.groups.models import Group, GroupMember

User = get_user_model()
user = User.objects.get(email='jerryjo0802@gmail.com')
group = Group.objects.get(id='921630a9-41d6-436e-8da4-492f01446bdc')

member = GroupMember.objects.filter(user=user, group=group).first()
if member:
    print(f'✅ 用戶是群組成員')
    print(f'  - 角色: {member.role.display_name}')
else:
    print('❌ 用戶不是群組成員')
"
```

### 問題 3: group_id required

**解決**: 確認 `config.py` 中的 `GROUP_ID` 已設定

## 🎯 成功指標

- [ ] 配置檔案載入成功
- [ ] JWT 認證通過
- [ ] PDF 上傳成功
- [ ] Report 建立成功
- [ ] ConstructionProject 建立成功
- [ ] 通知已觸發（如果是危險等級）

---

**測試日期**: 2025-12-31
**測試帳號**: jerryjo0802@gmail.com
**測試群組**: 測試上傳群組
