# geoBingAn PDF 同步工具

自動從台北市政府同步建案 PDF 並上傳到 geoBingAn Backend API 進行 AI 分析。

## 📋 功能說明

這是一個**獨立的外部工具**，透過 HTTP API 與 geoBingAn Backend 互動：

1. **sync_permits.py** - 從台北市政府同步建案 PDF 到 Google Drive
2. **upload_pdfs.py** - 上傳 PDF 到 geoBingAn Backend API
3. **retry_failed.py** - 重試失敗的上傳

> **注意**：所有 AI 分析由 Backend 處理（使用 Gemini 3.0），此工具只負責檔案同步和上傳。

---

## 🚀 快速開始

### 1. 安裝依賴

```bash
pip install -r requirements.txt
```

### 2. 設定 Google Drive 認證

```bash
# 複製範例設定檔
cp credentials.json.example credentials.json

# 編輯 credentials.json，填入你的 Service Account 金鑰
```

### 3. 設定環境變數（可選）

```bash
# Google Drive 設定
export SHARED_DRIVE_ID=0AIvp1h-6BZ1oUk9PVA

# geoBingAn Backend API
export GEOBINGAN_API_URL=http://localhost:8000/api/reports/upload-file/

# 過濾設定
export DAYS_AGO=7                    # 處理最近幾天的 PDF
export MAX_UPLOADS=100               # 單次最多上傳幾個
export DELAY_BETWEEN_UPLOADS=20      # 上傳間隔（秒）
```

### 4. 執行

```bash
# 步驟 1: 同步建案 PDF from 台北市政府
python3 sync_permits.py

# 步驟 2: 上傳到 geoBingAn Backend API
python3 upload_pdfs.py

# 如有失敗，重試
python3 retry_failed.py
```

---

## ⚙️ 設定說明

### Google Drive Service Account

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立 Service Account
3. 下載 JSON 金鑰
4. 將金鑰儲存為 `credentials.json`
5. 將 Service Account email 加入共享雲端的協作者

### geoBingAn Backend API

工具透過以下 API 端點與 Backend 互動：

- `POST /api/reports/upload-file/` - 上傳 PDF 進行 AI 分析

**Request**:
```bash
curl -X POST http://localhost:8000/api/reports/upload-file/ \
  -F "file=@example.pdf" \
  -F "scenario_id=construction_safety_pdf" \
  -F "language=zh-TW" \
  -F "save_to_report=true" \
  -F "additional_context=建案代碼: 113建字第0001號"
```

**Response**:
```json
{
  "success": true,
  "report_id": "uuid",
  "construction_project": {
    "project_code": "113建字第0001號",
    "monitoring_report_id": "uuid"
  }
}
```

---

## 📅 自動化排程

### 使用 Cron（推薦）

```bash
# 編輯 crontab
crontab -e

# 每週日凌晨 2:00 執行
0 2 * * 0 cd /path/to/geoBingAn-pdf-sync-tool && python3 upload_pdfs.py >> /tmp/pdf_sync.log 2>&1
```

### 使用 Systemd Timer

建立 `/etc/systemd/system/pdf-sync.service`:
```ini
[Unit]
Description=geoBingAn PDF Sync
After=network.target

[Service]
Type=oneshot
User=your-user
WorkingDirectory=/path/to/geoBingAn-pdf-sync-tool
ExecStart=/usr/bin/python3 upload_pdfs.py
```

建立 `/etc/systemd/system/pdf-sync.timer`:
```ini
[Unit]
Description=Weekly PDF Sync Timer

[Timer]
OnCalendar=Sun *-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

啟動:
```bash
sudo systemctl enable pdf-sync.timer
sudo systemctl start pdf-sync.timer
```

---

## 📁 檔案結構

```
geoBingAn-pdf-sync-tool/
├── README.md                    # 本文件
├── requirements.txt             # Python 依賴
├── sync_permits.py              # PDF 同步腳本
├── upload_pdfs.py               # 上傳到 Backend API
├── retry_failed.py              # 失敗重試
├── credentials.json.example     # Service Account 範例
├── config.py                    # 設定檔（可選）
├── state/                       # 狀態追蹤
│   └── .gitkeep
└── docs/                        # 詳細文檔
    └── API.md                   # API 說明文檔
```

---

## 🔍 狀態追蹤

工具使用 JSON 檔案追蹤處理狀態：

### `state/sync_permits_progress.json`
記錄已同步的建案：
```json
{
  "processed": ["112建字第0001號", ...],
  "errors": [...],
  "restricted": [...]
}
```

### `state/uploaded_pdfs.json`
記錄已上傳的 PDF：
```json
{
  "uploaded_files": ["建案資料夾/檔案名稱.pdf", ...],
  "errors": [
    {
      "folder": "建案資料夾",
      "file": "檔案名稱.pdf",
      "file_id": "Google Drive ID"
    }
  ]
}
```

**重置狀態**：
```bash
# 清除所有狀態（重新處理所有檔案）
rm state/*.json
```

---

## 🔧 故障排除

### 1. Google Drive 認證失敗

```
❌ 找不到 Service Account 金鑰
```

**解決**：
```bash
# 確認檔案存在
ls -la credentials.json

# 確認 JSON 格式正確
python3 -m json.tool credentials.json
```

### 2. Backend API 連線失敗

```
❌ API 錯誤 (Connection refused)
```

**解決**：
```bash
# 確認 Backend 運行中
curl http://localhost:8000/health/

# 檢查 API URL 設定
echo $GEOBINGAN_API_URL
```

### 3. PDF 上傳失敗

檢查 Backend 日誌：
```bash
# Docker 環境
docker-compose logs -f web

# 直接運行
tail -f logs/django.log
```

---

## 📊 監控

### 查看處理統計

```bash
# 已同步建案數量
python3 -c "
import json
with open('state/sync_permits_progress.json') as f:
    data = json.load(f)
    print(f'已處理建案: {len(data[\"processed\"])}')
    print(f'錯誤: {len(data[\"errors\"])}')
"

# 已上傳 PDF 數量
python3 -c "
import json
with open('state/uploaded_pdfs.json') as f:
    data = json.load(f)
    print(f'已上傳: {len(data[\"uploaded_files\"])}')
    print(f'失敗: {len(data[\"errors\"])}')
"
```

### 查看 Backend 資料庫

```bash
# 透過 Backend API 查詢
curl http://localhost:8000/api/construction-projects/ | jq '.count'
```

---

## 🔐 安全性

### 敏感檔案管理

**不要提交到 Git**：
- `credentials.json` - Service Account 金鑰
- `state/*.json` - 可能包含敏感資訊

**.gitignore** 範例：
```gitignore
credentials.json
state/*.json
*.log
__pycache__/
```

### 權限最小化

Service Account 只需要以下權限：
- Google Drive API - 讀取權限
- 共享雲端 - 檢視者權限

---

## 🆘 獲取幫助

### 問題回報

1. **工具問題**：建立 Issue 在此 repository
2. **Backend 問題**：前往 [geoBingAn_v2_backend](https://github.com/GeoThings/geoBingAn_v2_backend)
3. **API 問題**：查看 Backend API 文檔

### 相關連結

- [geoBingAn Backend Repository](https://github.com/GeoThings/geoBingAn_v2_backend)
- [Backend API 文檔](https://github.com/GeoThings/geoBingAn_v2_backend/blob/main/docs/BATCH_PDF_UPLOAD_GUIDE.md)

---

## 📝 版本歷史

### v1.0.0 (2025-12-31)
- ✅ 初始版本
- ✅ 支援從台北市政府同步建案 PDF
- ✅ 支援上傳到 geoBingAn Backend API
- ✅ 狀態追蹤和失敗重試
- ✅ 7 天時間過濾

---

## 📄 授權

MIT License

---

**維護者**: geoBingAn Team
**最後更新**: 2025-12-31
