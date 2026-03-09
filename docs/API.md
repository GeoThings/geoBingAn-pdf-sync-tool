# geoBingAn Backend API 說明

此工具透過 geoBingAn Backend API 上傳 PDF 進行 AI 分析。

> ⚠️ **重要**: 請使用正確的 API 端點 `/api/reports/construction-reports/upload/`
> 舊端點 `/api/reports/upload-file/` 已棄用。

## API 端點

### POST /api/reports/construction-reports/upload/

上傳 PDF 檔案到 Backend 進行 AI 分析。

**URL**: `https://riskmap.today/api/reports/construction-reports/upload/`

**Method**: `POST`

**Content-Type**: `multipart/form-data`

**Authentication**: `Bearer Token` (JWT)

---

## Request Headers

| Header | 必填 | 說明 |
|--------|------|------|
| `Authorization` | ✅ | `Bearer <JWT_TOKEN>` |
| `Content-Type` | ✅ | `multipart/form-data` |

---

## Request Parameters

| 參數 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `file` | File | ✅ | PDF 檔案 |
| `group_id` | String | ✅ | 群組 ID |

---

## Request Example

### cURL

```bash
curl -X POST https://riskmap.today/api/reports/construction-reports/upload/ \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -F "file=@example.pdf" \
  -F "group_id=your-group-id"
```

### Python (requests)

```python
import requests

url = 'https://riskmap.today/api/reports/construction-reports/upload/'
headers = {
    'Authorization': f'Bearer {JWT_TOKEN}'
}
files = {'file': ('example.pdf', open('example.pdf', 'rb'), 'application/pdf')}
data = {
    'group_id': 'your-group-id'
}

response = requests.post(url, headers=headers, files=files, data=data, timeout=120)
print(response.json())
```

---

## Response Format

### 成功 (200/202)

```json
{
  "id": "d25a2e1c-af27-484d-ac05-ae361296b96e",
  "report_id": "d25a2e1c-af27-484d-ac05-ae361296b96e",
  "parse_status": "processing",
  "message": "報告上傳成功，正在使用 Gemini 進行 AI 分析..."
}
```

### 失敗 (4xx/5xx)

```json
{
  "error": "錯誤訊息",
  "detail": "詳細錯誤資訊"
}
```

---

## Response Fields

| 欄位 | 類型 | 說明 |
|------|------|------|
| `id` | String (UUID) | 報告 ID |
| `report_id` | String (UUID) | 報告 ID (同上) |
| `parse_status` | String | 解析狀態 (`processing`, `completed`, `failed`) |
| `message` | String | 狀態訊息 |

---

## Error Codes

| HTTP Status | 錯誤原因 |
|-------------|----------|
| `400 Bad Request` | 參數錯誤或檔案格式不正確 |
| `401 Unauthorized` | JWT Token 過期或無效 |
| `403 Forbidden` | 無權限或檔案已存在 |
| `413 Payload Too Large` | 檔案過大 |
| `500 Internal Server Error` | Backend 內部錯誤 |
| `504 Gateway Timeout` | AI 處理超時 |

---

## JWT Token 刷新

Token 過期時需要刷新：

**端點**: `POST https://riskmap.today/api/auth/auth/refresh_token/`

```python
import requests

response = requests.post(
    'https://riskmap.today/api/auth/auth/refresh_token/',
    json={'refresh_token': REFRESH_TOKEN},
    timeout=30
)

if response.status_code == 200:
    new_token = response.json().get('access_token')
```

---

## Backend Processing

### AI 分析流程

1. **檔案上傳** → Backend 接收 PDF
2. **PDF 解析** → 提取文字和圖片
3. **AI 分析** → 使用 **Gemini 2.5 Pro** 進行分析
4. **資料儲存** → 建立 Report 和 ConstructionProject 記錄
5. **回傳結果** → 返回分析結果

---

## Rate Limiting

建議在上傳時加入延遲避免觸發 rate limit：

```python
import time

for pdf in pdfs:
    upload_pdf(pdf)
    time.sleep(2)  # 每次上傳間隔 2 秒
```

---

## Timeout Settings

建議設定較長的 timeout：

```python
response = requests.post(url, files=files, data=data, timeout=120)  # 2 分鐘
```

---

## 相關文檔

- [Backend Repository](https://github.com/GeoThings/geoBingAn_v2_backend)
- [問題排解指南](troubleshooting.md)

---

**最後更新**: 2026-03-09
