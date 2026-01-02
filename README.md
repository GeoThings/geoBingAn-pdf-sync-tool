# geoBingAn PDF 同步工具

自動從台北市政府同步建案 PDF 並上傳到 geoBingAn Backend API 進行 AI 分析。

## 📋 功能說明

這是一個**獨立的外部工具**，透過 HTTP API 與 geoBingAn Backend 互動：

1. **sync_permits.py** - 從台北市政府同步建案 PDF 到 Google Drive
2. **upload_pdfs.py** - 從 Google Drive 上傳最近 7 天的 PDF 到 geoBingAn Backend API
3. **upload_attachments.py** - 為已建立的 Reports 補充 PDF 附件並上傳到 S3
4. **check_upload_status.py** - 檢查上傳狀態和資料庫記錄
5. **retry_failed.py** - 重試失敗的上傳（保留，未使用）

> **注意**：所有 AI 分析由 Backend 處理（使用 Gemini 2.5/3.0 Pro），此工具只負責檔案同步和上傳。

### 🎯 最新測試結果（2026-01-02）

**累計上傳統計：**
- ✅ 成功建立 Reports：**13 個**
- ✅ 成功建立建案：**9 個**
- ⚠️ 驗證失敗：3 個簡化週報（已記錄）
- 📊 成功率：**81.25%**

詳見 [logs/upload_history.md](logs/upload_history.md)

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
# 步驟 1: 同步建案 PDF from 台北市政府（可選）
python3 sync_permits.py

# 步驟 2: 從 Google Drive 上傳最近 7 天的 PDF
python3 upload_pdfs.py

# 步驟 3: 為已建立的 Reports 補充 PDF 附件（可選）
python3 upload_attachments.py

# 步驟 4: 檢查上傳狀態
python3 check_upload_status.py
```

**注意事項：**
- `upload_pdfs.py` 會自動過濾最近 7 天更新的 PDF
- 預設上傳數量限制為 10 個（可在腳本中修改 `MAX_UPLOADS`）
- 每次上傳間隔 20 秒以避免 API 速率限制
- 上傳狀態會記錄在 `state/uploaded_to_geobingan_7days.json`

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

工具已設定為每週一早上 9:00 自動執行：

```bash
# 查看當前 cron 設定
crontab -l

# 當前排程：每週一早上 9:00
0 9 * * 1 /Users/geothingsmacbookair/Documents/GitHub/geoBingAn-pdf-sync-tool/run_weekly_sync.sh
```

**執行流程**：
1. `sync_permits.py` - 從台北市政府網站同步最新建案 PDF 到 Google Drive
2. `upload_pdfs.py` - 上傳最近 7 天更新的 PDF 到 geoBingAn Backend

**日誌管理**：
- 執行日誌位於 `logs/` 目錄
- 日誌檔案格式：`weekly_sync_YYYYMMDD_HHMMSS.log`
- 自動清理超過 30 天的舊日誌

**手動執行**：
```bash
# 測試執行完整流程
./run_weekly_sync.sh

# 查看最新日誌
tail -f logs/weekly_sync_*.log
```

**修改排程時間**：
```bash
# 編輯 crontab
crontab -e

# Cron 格式：分 時 日 月 星期
# 範例：每週三下午 3:00
0 15 * * 3 /path/to/run_weekly_sync.sh
```

### 使用 Systemd Timer（進階）

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
├── README.md                               # 本文件
├── requirements.txt                        # Python 依賴
├── credentials.json                        # Service Account 金鑰（需自行建立）
├── .env                                    # 環境變數（需自行建立）
│
├── sync_permits.py                         # PDF 同步腳本（從台北市政府）
├── upload_pdfs.py                          # 上傳 PDF 到 Backend API
├── upload_attachments.py                   # 補充 PDF 附件到已建立的 Reports
├── check_upload_status.py                  # 檢查上傳狀態
├── retry_failed.py                         # 失敗重試（保留）
│
├── state/                                  # 狀態追蹤
│   ├── uploaded_to_geobingan_7days.json   # 7天上傳記錄
│   ├── uploaded_to_geobingan_7days.json.backup  # 備份
│   └── sync_permits_progress.json         # 同步進度
│
└── logs/                                   # 日誌記錄
    └── upload_history.md                   # 上傳歷史記錄
```

---

## 🔍 狀態追蹤

工具使用 JSON 檔案追蹤處理狀態：

### `state/uploaded_to_geobingan_7days.json`
記錄最近 7 天上傳的 PDF：
```json
{
  "uploaded_files": [
    "112建字第0087號/北士科服務中心監測日報20251229.pdf",
    "111建字第0252號/1141222 25觀測報告.pdf",
    ...
  ],
  "errors": []
}
```

### `state/sync_permits_progress.json`
記錄已同步的建案（從台北市政府）：
```json
{
  "processed": ["112建字第0001號", ...],
  "errors": [...],
  "restricted": [...]
}
```

### `logs/upload_history.md`
完整的上傳歷史記錄，包括：
- 每次上傳的時間和結果
- 成功建立的 Reports 列表
- 失敗原因分析
- 統計資訊

**重置狀態**：
```bash
# 清除 7 天上傳記錄（重新處理最近 7 天的檔案）
rm state/uploaded_to_geobingan_7days.json

# 清除所有狀態
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

### v1.1.0 (2026-01-02)
- ✅ 新增 `upload_attachments.py` - 補充 PDF 附件功能
- ✅ 新增 `check_upload_status.py` - 上傳狀態檢查工具
- ✅ 完成自動上傳測試：13 個 Reports 成功建立
- ✅ 新增完整的上傳歷史記錄（`logs/upload_history.md`）
- ✅ 改進狀態追蹤：使用 `uploaded_to_geobingan_7days.json`
- ✅ 支援自動建立 ConstructionProject 和 ProjectMonitoringReport
- ✅ 新增備份機制和錯誤處理
- 📊 測試結果：81.25% 成功率（13/16）

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
**最後更新**: 2026-01-02
