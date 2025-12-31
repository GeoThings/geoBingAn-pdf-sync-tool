# geoBingAn Backend API 說明

此工具透過 geoBingAn Backend API 上傳 PDF 進行 AI 分析。

## API 端點

### POST /api/reports/upload-file/

上傳 PDF 檔案到 Backend 進行 AI 分析。

**URL**: `http://localhost:8000/api/reports/upload-file/`

**Method**: `POST`

**Content-Type**: `multipart/form-data`

---

## Request Parameters

| 參數 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `file` | File | ✅ | PDF 檔案 |
| `scenario_id` | String | ✅ | 場景 ID（固定為 `construction_safety_pdf`） |
| `language` | String | ✅ | 語言（`zh-TW`, `en`, 等） |
| `save_to_report` | Boolean | ⚠️ | 是否儲存到報告（建議 `true`） |
| `additional_context` | String | ⚠️ | 額外資訊（例如：建案代碼） |

---

## Request Example

### cURL

```bash
curl -X POST http://localhost:8000/api/reports/upload-file/ \
  -F "file=@example.pdf" \
  -F "scenario_id=construction_safety_pdf" \
  -F "language=zh-TW" \
  -F "save_to_report=true" \
  -F "additional_context=建案代碼: 113建字第0001號"
```

### Python (requests)

```python
import requests

url = 'http://localhost:8000/api/reports/upload-file/'
files = {'file': open('example.pdf', 'rb')}
data = {
    'scenario_id': 'construction_safety_pdf',
    'language': 'zh-TW',
    'save_to_report': True,
    'additional_context': '建案代碼: 113建字第0001號'
}

response = requests.post(url, files=files, data=data)
print(response.json())
```

---

## Response Format

### 成功 (200 OK)

```json
{
  "success": true,
  "report_id": "d25a2e1c-af27-484d-ac05-ae361296b96e",
  "construction_project": {
    "project_code": "113建字第0001號",
    "project_name": "建案名稱",
    "monitoring_report_id": "1739e2b7-9762-44d4-a2e8-65741d05e4d2"
  },
  "analysis": {
    "summary": "AI 分析摘要...",
    "confidence_score": 95.0
  }
}
```

### 失敗 (4xx/5xx)

```json
{
  "success": false,
  "error": "錯誤訊息",
  "details": "詳細錯誤資訊"
}
```

---

## Response Fields

| 欄位 | 類型 | 說明 |
|------|------|------|
| `success` | Boolean | 是否成功 |
| `report_id` | String (UUID) | 報告 ID |
| `construction_project` | Object | 建案資訊 |
| `construction_project.project_code` | String | 建案代碼 |
| `construction_project.project_name` | String | 建案名稱 |
| `construction_project.monitoring_report_id` | String (UUID) | 監測報告 ID |
| `analysis` | Object | AI 分析結果摘要 |
| `analysis.summary` | String | 分析摘要 |
| `analysis.confidence_score` | Float | 信心分數 (0-100) |

---

## Error Codes

| HTTP Status | 錯誤原因 |
|-------------|----------|
| `400 Bad Request` | 參數錯誤或檔案格式不正確 |
| `401 Unauthorized` | 未授權（如需要認證） |
| `413 Payload Too Large` | 檔案過大 |
| `500 Internal Server Error` | Backend 內部錯誤 |
| `503 Service Unavailable` | AI 服務暫時無法使用 |

---

## Backend Processing

### AI 分析流程

1. **檔案上傳** → Backend 接收 PDF
2. **PDF 解析** → 提取文字和圖片
3. **AI 分析** → 使用 **Gemini 3.0** 進行分析
4. **資料儲存** → 建立 Report 和 ConstructionProject 記錄
5. **回傳結果** → 返回分析結果

### 資料模型

Backend 建立以下記錄：

```python
# Report 模型
Report(
    id=UUID,
    title="建案監測報告",
    report_type="manual_web",
    content=<JSON 格式的完整 AI 分析>,
    metadata={
        "confidence_score": 95.0,
        "analysis_model": "gemini-3.0",
        "scenario_id": "construction_safety_pdf",
        "source_file": "example.pdf"
    },
    status="pending"
)

# ConstructionProject 模型
ConstructionProject(
    project_code="113建字第0001號",
    project_name="建案名稱",
    location=<GIS Point>,
    ...
)

# ProjectMonitoringReport 模型
ProjectMonitoringReport(
    project=<ConstructionProject>,
    report=<Report>,
    pdf_file_name="example.pdf",
    ai_analysis_completed=True,
    ai_analysis_result=<完整 JSON>,
    processing_status="completed"
)
```

---

## Rate Limiting

建議在上傳時加入延遲避免觸發 rate limit：

```python
import time

for pdf in pdfs:
    upload_pdf(pdf)
    time.sleep(20)  # 每次上傳間隔 20 秒
```

---

## Timeout Settings

建議設定較長的 timeout：

```python
response = requests.post(url, files=files, data=data, timeout=300)  # 5 分鐘
```

---

## 相關文檔

- [Backend Repository](https://github.com/GeoThings/geoBingAn_v2_backend)
- [完整 API 文檔](https://github.com/GeoThings/geoBingAn_v2_backend/blob/main/docs/API.md)
