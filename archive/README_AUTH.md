# geoBingAn PDF 自動上傳 - 認證設定指南

## 📋 概述

此工具現已整合 JWT 認證，所有上傳都會使用指定的用戶帳號和群組。

## 🔐 認證資訊

### 當前配置（測試帳號）

- **用戶 Email**: `jerryjo0802@gmail.com`
- **用戶 ID**: `a3b1da69-6640-4aba-9267-2ccc2f5b9186`
- **群組名稱**: `測試上傳群組`
- **群組 ID**: `921630a9-41d6-436e-8da4-492f01446bdc`
- **用戶角色**: Owner（擁有者）
- **JWT Token 有效期**: 365 天（至 2026年12月31日）

## 🚀 使用方式

### 1. 確認配置檔案

配置檔案已自動建立在 `config.py`，包含：
- JWT Token
- 用戶資訊
- 群組 ID
- API URL

**⚠️ 注意**: `config.py` 已加入 `.gitignore`，不會被提交到 Git。

### 2. 執行上傳

```bash
python upload_pdfs.py
```

腳本會自動：
1. 載入 `config.py` 中的認證資訊
2. 使用 JWT Token 認證
3. 將 PDF 上傳到指定的群組
4. 顯示用戶資訊確認

### 3. 預期輸出

```
✅ 已載入認證配置（用戶: jerryjo0802@gmail.com）
🔑 初始化 Google Drive API (Service Account)
✅ 已初始化 (your-service-account@project.iam.gserviceaccount.com)
...
```

## 📋 認證流程

```
1. upload_pdfs.py 啟動
   ↓
2. 載入 config.py
   - JWT_TOKEN
   - GROUP_ID
   ↓
3. 掃描 Google Drive PDF
   ↓
4. 上傳到 geoBingAn API
   - Header: Authorization: Bearer {JWT_TOKEN}
   - Data: group_id={GROUP_ID}
   ↓
5. API 驗證 JWT
   - 提取 user_id
   - 驗證用戶權限
   ↓
6. 建立 Report
   - created_by: jerryjo0802@gmail.com
   - group: 測試上傳群組
   ↓
7. AI 分析 PDF
   ↓
8. 如果 safety_status 是 'action' 或 'critical':
   ↓
9. 🔔 觸發通知
   - 通知群組的 owner/admin/moderator
   - WebSocket 即時推播
   - Email 通知
```

## 🔧 變更帳號

如需使用不同帳號：

### 方法 1: 修改 config.py

直接編輯 `config.py` 更改：
- `JWT_TOKEN`
- `GROUP_ID`
- `USER_EMAIL`

### 方法 2: 生成新的 JWT Token

1. 在 Docker 容器中執行：

```bash
docker exec geobingan-web python manage.py shell -c "
from django.contrib.auth import get_user_model
from django.conf import settings
import jwt
from datetime import datetime, timedelta

User = get_user_model()
user = User.objects.get(email='your-email@example.com')

payload = {
    'user_id': str(user.id),
    'type': 'access',
    'exp': datetime.utcnow() + timedelta(days=365),
    'iat': datetime.utcnow()
}

token = jwt.encode(payload, settings.SECRET_KEY, algorithm='HS256')
print('JWT Token:', token)
"
```

2. 更新 `config.py` 中的 `JWT_TOKEN`

## ⚠️ 安全注意事項

1. **JWT Token 保密**:
   - Token 擁有完整用戶權限
   - 不要分享給他人
   - 不要提交到公開的 Git repository

2. **定期更換 Token**:
   - 建議每 90 天更換一次
   - 如懷疑洩露立即重新生成

3. **群組權限**:
   - 確保用戶是目標群組的成員
   - 建議使用專用的「自動上傳」群組

## 🔍 故障排除

### 401 Unauthorized

```json
{"detail": "Authentication credentials were not provided."}
```

**原因**: JWT Token 無效或過期
**解決**: 重新生成 JWT Token

### 403 Forbidden

```json
{"error": "Permission denied"}
```

**原因**: 用戶沒有權限存取該群組
**解決**: 確認用戶是群組成員且有上傳權限

### 400 Bad Request (group_id required)

```json
{"error": "group_id is required for authenticated uploads"}
```

**原因**: 缺少 group_id 參數
**解決**: 確認 `config.py` 中的 `GROUP_ID` 已設定

## 📞 技術支援

如有問題，請檢查：
1. JWT Token 是否正確
2. 用戶是否已加入群組
3. Docker 容器是否正常運行
4. API URL 是否正確（預設 http://localhost:8000）

---

**更新日期**: 2025-12-31
**認證方式**: JWT Bearer Token
**Token 有效期**: 365 天
