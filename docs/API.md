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

**Authentication**: `Bearer Token` (JWT，由 `jwt_auth.py` 模組自動管理刷新)

---

## Request Parameters

| 參數 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `file` | File | ✅ | PDF 檔案 |
| `group_id` | String | ✅ | 群組 ID |
| `report_type` | String | ✅ | 報告類型（`weekly`, `daily`, `monthly`, `incident`, `inspection`） |
| `primary_language` | String | ✅ | 語言（`zh-TW`） |

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
    'group_id': 'your-group-id',
    'report_type': 'weekly',
    'primary_language': 'zh-TW'
}

response = requests.post(url, headers=headers, files=files, data=data, timeout=600)
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

| HTTP Status | 錯誤原因 | 工具行為 |
|-------------|----------|----------|
| `400 Bad Request` | 參數錯誤或檔案格式不正確 | 不重試 |
| `401 Unauthorized` | JWT Token 過期或無效 | 自動刷新 Token 後重試一次 |
| `403 Forbidden` | 無權限或檔案已存在 | 不重試 |
| `502 Bad Gateway` | 閘道器錯誤，PDF 可能已送達 | 不重試（視為 processing） |
| `503 Service Unavailable` | 伺服器暫時不可用 | 指數退避重試（5s/15s/30s） |
| `504 Gateway Timeout` | AI 處理超時，PDF 可能已送達 | 不重試（視為 processing） |

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

上傳間隔 0.5 秒（後端為非同步 AI 處理，不需長等待）。
Timeout 建議設為 600 秒（10 分鐘），大型 PDF 的 AI 分析需要較長時間。

---

## 相關文檔

- [系統架構設計](architecture.md)
- [問題排解指南](troubleshooting.md)
- [Backend Repository](https://github.com/GeoThings/geoBingAn_v2_backend)

---

**最後更新**: 2026-04-02
