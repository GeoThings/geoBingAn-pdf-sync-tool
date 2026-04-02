# 問題排解指南

本文件記錄開發與維護過程中遇到的問題及解決方案。

## 目錄

1. [腳本執行卡住（死鎖問題）](#1-腳本執行卡住死鎖問題)
2. [JWT Token 過期](#2-jwt-token-過期)
3. [API 端點錯誤](#3-api-端點錯誤)
4. [HTTP 狀態碼處理](#4-http-狀態碼處理)
5. [504 Gateway Timeout](#5-504-gateway-timeout)
6. [PDF 檔案缺少副檔名](#6-pdf-檔案缺少副檔名)

---

## 1. 腳本執行卡住

### 症狀
- 腳本無回應，CPU 使用率低

### 解決方案
v3.1+ 已修復死鎖問題。`save_state()` 現在使用 `flock` + read-merge-write + atomic replace，不再有 thread-level 死鎖風險。如仍卡住，可能是網路逾時 — 檢查 Google Drive API 連線。

---

## 2. JWT Token 過期

### 症狀
- API 回傳 401 Unauthorized

### 解決方案
v3.1+ 的 `jwt_auth.py` 模組自動處理 Token 刷新（過期前 5 分鐘觸發）。

**如果 Refresh Token 也過期（7 天有效期）：**
1. 登入 riskmap.today
2. F12 → Application → Local Storage
3. 複製 `access_token` 和 `refresh_token`
4. 更新 `.env` 中的 `JWT_TOKEN` 和 `REFRESH_TOKEN`

---

## 3. API 端點錯誤

### 症狀
- 上傳回傳 500 Internal Server Error
- 錯誤訊息：`FormTemplate matching query does not exist` 或 `NOT NULL constraint failed`

### 原因
使用了錯誤的 API 端點 `/api/reports/upload-file/`，該端點有後端 Bug。

### 解決方案
改用網頁介面使用的端點：

```python
# 錯誤
GEOBINGAN_API_URL = 'https://riskmap.today/api/reports/upload-file/'

# 正確
GEOBINGAN_API_URL = 'https://riskmap.today/api/reports/construction-reports/upload/'
```

### 如何發現正確的端點
1. 開啟瀏覽器開發者工具 (F12)
2. 切換到 Network 分頁
3. 在網頁上手動上傳 PDF
4. 觀察實際發送的 API 請求

---

## 4. HTTP 狀態碼處理

### 症狀
- 上傳成功但腳本顯示錯誤
- 狀態碼 202 被當作失敗處理

### 原因
原本只檢查 200 和 201 狀態碼：

```python
# 錯誤
if response.status_code in [200, 201]:
    # 成功
```

### 解決方案
加入 202 (Accepted) 狀態碼：

```python
# 正確
if response.status_code in [200, 201, 202]:
    # 成功 - 202 表示請求已接受，正在處理中
```

### 常見狀態碼說明
| 狀態碼 | 意義 | 處理方式 |
|--------|------|----------|
| 200 | OK | 成功 |
| 201 | Created | 成功建立資源 |
| 202 | Accepted | 請求已接受，非同步處理中 |
| 401 | Unauthorized | Token 過期，需要刷新 |
| 500 | Server Error | 後端錯誤，檢查端點或參數 |
| 504 | Gateway Timeout | 請求超時，可能檔案太大 |

---

## 5. 504 Gateway Timeout

### 症狀
- 大型 PDF 上傳時回傳 504 錯誤
- Nginx 超時錯誤

### 原因
- Nginx 預設超時時間約 60 秒
- 大型 PDF 處理時間超過限制

### 解決方案

**方案 A：減小檔案大小**
- 壓縮 PDF 後再上傳
- 分批上傳大型報告

**方案 B：增加請求超時**（客戶端）
```python
response = requests.post(
    url,
    files=files,
    headers=headers,
    timeout=120  # 增加到 120 秒
)
```

**方案 C：調整 Nginx 設定**（需要伺服器權限）
```nginx
# /etc/nginx/nginx.conf
proxy_read_timeout 300;
proxy_connect_timeout 300;
proxy_send_timeout 300;
```

---

## 6. PDF 檔案缺少副檔名

### 症狀
- 上傳回傳 400 Bad Request 錯誤
- 錯誤訊息：`{"file":["只接受 PDF 或 JSON 檔案"]}`
- 檔案確實是 PDF（MIME type 正確）但上傳失敗

### 原因
某些 Google Drive 上的 PDF 檔案沒有 `.pdf` 副檔名。後端 API 根據檔名副檔名判斷檔案類型，因此拒絕上傳。

```
# 問題檔案範例
中華電信濱江資料中心監測報告-(1141226)    ← 沒有 .pdf 副檔名
MIME Type: application/pdf                   ← 實際上是 PDF 檔案
```

### 解決方案
腳本已自動處理：上傳時檢查檔名，如果沒有 `.pdf` 副檔名會自動補上。

```python
# upload_to_geobingan() 函數中
if not file_name.lower().endswith('.pdf'):
    file_name = file_name + '.pdf'
    print(f"  ℹ️  自動加上 .pdf 副檔名: {file_name}")
```

### 如何檢查 Google Drive 檔案
```python
python3 -c "
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ... 初始化 service ...

file = service.files().get(
    fileId='FILE_ID',
    supportsAllDrives=True,
    fields='name, mimeType, fileExtension'
).execute()

print(f'檔名: {file.get(\"name\")}')
print(f'MIME: {file.get(\"mimeType\")}')
print(f'副檔名: {file.get(\"fileExtension\", \"無\")}')
"
```

---

## 除錯技巧

### 1. 啟用詳細輸出
```bash
PYTHONUNBUFFERED=1 python3 upload_pdfs.py 2>&1 | tee upload.log
```

### 2. 檢查 Token 狀態
```python
python3 -c "
from config import JWT_TOKEN
import base64, json, time

payload = JWT_TOKEN.split('.')[1]
payload += '=' * (4 - len(payload) % 4)
data = json.loads(base64.urlsafe_b64decode(payload))

exp = data.get('exp', 0)
print(f'過期時間: {time.ctime(exp)}')
print(f'剩餘: {int((exp - time.time()) / 60)} 分鐘')
"
```

### 3. 測試 API 連線
```bash
curl -X GET "https://riskmap.today/api/reports/construction-reports/" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json"
```

### 4. 檢查上傳狀態
```python
python3 -c "
import requests
from config import JWT_TOKEN, GROUP_ID

response = requests.get(
    f'https://riskmap.today/api/reports/construction-reports/?group_id={GROUP_ID}&page_size=5',
    headers={'Authorization': f'Bearer {JWT_TOKEN}'}
)
print(response.json())
"
```

---

---

**最後更新**: 2026-04-02
