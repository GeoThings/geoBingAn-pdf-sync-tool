# geoBingAn PDF 同步上傳工具

自動從台北市政府建管處同步建案 PDF，並上傳到 geoBingAn Backend API 建立監測報告。

## 📊 最新同步結果（2026-04-02）

**每週同步：** ✅ 完成

| 項目 | 狀態 | 數量 |
|---------|------|--------|
| 建案監測 | ✅ 已同步 | 292 個建案（234 已處理） |
| Google Drive PDF | ✅ 掃描完成 | 11,624 個 |
| 建照資料夾 | ✅ 掃描完成 | 1,000 個 |
| 農曆新年後上傳 | ✅ 完成 | 56 個 PDF 上傳至究平安 |
| 究平安報告對應 | ✅ 成功 | 200 筆 API 報告 |
| JWT 自動刷新 | ✅ 正常 | Token 過期自動更新 |

### 📋 線上追蹤報告

👉 **[建照監測追蹤報告](https://htmlpreview.github.io/?https://github.com/GeoThings/geoBingAn-pdf-sync-tool/blob/main/docs/index.html)** 👈

報告包含：建案同步狀態、究平安對應情況、警戒值標記、可搜尋篩選

**功能特色：**
- ✅ 檔名日期智慧解析（支援民國年/西元年 7 種格式）
- ✅ 依檔名日期過濾上傳（非 Google Drive 修改時間）
- ✅ JWT Token 自動刷新（過期前 5 分鐘自動更新）
- ✅ 智慧快取機制（自動偵測快取過期並重建）
- ✅ 跨 process 安全的 state 管理（flock + read-merge-write + atomic replace）
- ✅ 上傳重試機制（503 指數退避，502/504 不重試避免重複）
- ✅ JWT Token 共用模組（jwt_auth.py，thread-safe）
- ✅ Shell 級聯失敗保護（步驟失敗自動跳過下游）
- ✅ 支援每日 cron job 自動執行
- ✅ 線上追蹤報告自動更新
- ✅ 環境變數管理（.env 檔案，向後相容 fallback）
- ✅ 通知功能（LINE Notify / macOS）
- ✅ 自動化測試（pytest, 41 cases）

詳見文件：
- [建照監測追蹤報告](https://htmlpreview.github.io/?https://github.com/GeoThings/geoBingAn-pdf-sync-tool/blob/main/docs/index.html) 📊 線上即時查看
- [問題排解指南](docs/troubleshooting.md) 🔧 常見問題解決方案
- [系統架構設計](docs/architecture.md) 🏗️ 模組架構與設計決策

---

## 🎯 工具定位

**這是一個純粹的 PDF 同步上傳工具**，只負責：
1. ✅ 從台北市政府建管處同步建案 PDF 到 Google Drive
2. ✅ 從 Google Drive 上傳 PDF 到 geoBingAn Backend API
3. ✅ 定期自動執行（cron job）

**後端負責（不由此工具處理）**：
- AI 分析（Gemini 2.5/3.0 Pro）
- 建立 Report 和 ConstructionProject
- 檔案儲存（S3 或本地）
- JSON 資料儲存

---

## 🚀 快速開始

### 1. 安裝依賴

```bash
# 建立虛擬環境
python3 -m venv venv
source venv/bin/activate

# 安裝套件
pip install -r requirements.txt
```

### 2. 設定 Google Drive 認證

1. 前往 [Google Cloud Console](https://console.cloud.google.com/)
2. 建立 Service Account
3. 下載 JSON 金鑰並儲存為 `credentials.json`
4. 將 Service Account email 加入共享雲端的協作者

### 3. 設定環境變數

複製範例並填入你的認證資訊：
```bash
cp .env.example .env
# 編輯 .env 填入以下資訊
```

**.env 必要設定：**
```bash
# Google Drive
GOOGLE_CREDENTIALS=./credentials.json
SHARED_DRIVE_ID=your-shared-drive-id

# geoBingAn API 認證
JWT_TOKEN=your_access_token           # Access Token（會自動刷新）
REFRESH_TOKEN=your_refresh_token      # Refresh Token（有效期 7 天）
USER_EMAIL=your_email@example.com
USER_ID=your-user-id
GROUP_ID=your-group-id
GROUP_NAME=your-group-name

# API 端點
GEOBINGAN_API_URL=https://riskmap.today/api/reports/construction-reports/upload/
GEOBINGAN_REFRESH_URL=https://riskmap.today/api/auth/auth/refresh_token/

# 通知設定（可選）
LINE_NOTIFY_TOKEN=your-line-token     # 從 https://notify-bot.line.me/ 取得
ENABLE_MACOS_NOTIFY=true              # 啟用 macOS 系統通知
```

### 4. 執行

#### 快速執行指令（一行版本）：
```bash
# 完整流程（同步 + 上傳）
/Users/geothingsmacbookair/Documents/GitHub/geoBingAn-pdf-sync-tool/run_weekly_sync.sh

# 只上傳 PDF（跳過同步）
cd /Users/geothingsmacbookair/Documents/GitHub/geoBingAn-pdf-sync-tool && source venv/bin/activate && python3 upload_pdfs.py

# 只同步 PDF（從台北市政府）
cd /Users/geothingsmacbookair/Documents/GitHub/geoBingAn-pdf-sync-tool && source venv/bin/activate && python3 sync_permits.py
```

#### 手動執行（分步驟）：
```bash
cd /Users/geothingsmacbookair/Documents/GitHub/geoBingAn-pdf-sync-tool
source venv/bin/activate

# 步驟 1: 同步建案 PDF from 台北市政府
python3 sync_permits.py

# 步驟 2: 上傳最近 7 天的 PDF 到 Backend
python3 upload_pdfs.py
```

#### 自動執行（已設定 cron job）：
```bash
# 每週一早上 9:00 自動執行
# 查看排程: crontab -l
0 9 * * 1 /Users/geothingsmacbookair/Documents/GitHub/geoBingAn-pdf-sync-tool/run_weekly_sync.sh
```

#### 查看執行日誌：
```bash
# 最新日誌
tail -100 /Users/geothingsmacbookair/Documents/GitHub/geoBingAn-pdf-sync-tool/logs/weekly_sync_*.log

# 即時監控
tail -f /Users/geothingsmacbookair/Documents/GitHub/geoBingAn-pdf-sync-tool/logs/weekly_sync_*.log
```

---

## 📋 核心腳本說明

### `sync_permits.py`
從台北市政府建管處網站同步建案 PDF 到 Google Drive

**功能：**
- 下載台北市政府最新建案清單 PDF
- 解析 PDF 中的建案代碼和 Google Drive 連結
- 自動建立資料夾並下載 PDF 到共享雲端
- 斷點續傳，避免重複下載

**設定：**
```python
SERVICE_ACCOUNT_FILE = '/path/to/credentials.json'
SHARED_DRIVE_ID = '0AIvp1h-6BZ1oUk9PVA'
PDF_LIST_URL = 'https://www-ws.gov.taipei/...'
```

**狀態追蹤：**
```
state/sync_permits_progress.json
```

---

### `upload_pdfs.py`
從 Google Drive 上傳最近 7 天的 PDF 到 geoBingAn Backend API

**功能：**
- 掃描 Google Drive 中的建案 PDF
- 過濾最近 7 天更新的檔案
- 呼叫 Backend API `/api/reports/construction-reports/upload/` 上傳 PDF
- 自動建立 Report（由後端 Gemini 2.5 Pro 處理）
- JWT Token 自動刷新（過期前 5 分鐘）
- 智慧快取機制避免重複掃描
- 避免重複上傳

**設定：**
```python
DAYS_AGO = 7                    # 只上傳最近 7 天的 PDF
MAX_UPLOADS = 5                 # 單次最多上傳 5 個
DELAY_BETWEEN_UPLOADS = 2       # 上傳間隔 2 秒
AUTO_CONFIRM = True             # 自動確認模式
```

**狀態追蹤：**
```
state/uploaded_to_geobingan_7days.json
```

**API 呼叫：**
```bash
POST /api/reports/construction-reports/upload/
Headers:
  - Authorization: Bearer <JWT_TOKEN>
Body (multipart/form-data):
  - file: PDF 檔案
  - group_id: "user-group-id"
```

---

### `generate_permit_tracking_report.py`
生成建案追蹤報告，整合台北市政府建案清單與究平安系統資料

**功能：**
- 下載台北市政府最新建案清單 PDF
- 從究平安 API 取得所有監測報告
- 比對建案與系統報告的對應關係
- 載入警戒值資料並標記異常
- 生成 HTML 和 CSV 格式的追蹤報告

**輸出檔案：**
```
state/permit_tracking_report.html  # 互動式 HTML 報告
state/permit_tracking.csv          # CSV 資料匯出
```

**執行：**
```bash
python3 generate_permit_tracking_report.py
```

---

### `run_weekly_sync.sh`
自動執行完整流程的 Shell 腳本

**執行順序：**
1. `sync_permits.py` - 同步最新 PDF 到 Google Drive
2. `upload_pdfs.py` - 上傳最近 7 天的 PDF 到 Backend
3. `generate_permit_tracking_report.py` - 生成追蹤報告
4. Git push - 更新線上報告

**自動化功能：**
- 執行狀態追蹤（`state/sync_status.json`）
- 失敗時發送通知（LINE Notify / macOS）
- 完成時發送摘要通知
- 錯誤處理與自動清理

**日誌管理：**
- 日誌檔案：`logs/weekly_sync_YYYYMMDD_HHMMSS.log`
- 自動清理超過 30 天的舊日誌

**手動執行：**
```bash
./run_weekly_sync.sh

# 查看日誌
tail -f logs/weekly_sync_*.log
```

**查看執行歷史：**
```bash
# 查看狀態追蹤
python3 -c "from sync_status import SyncStatus; SyncStatus().print_summary()"
```

---

## ⏰ 定期執行設定

### Cron Job（已設定）

```bash
# 查看當前排程
crontab -l

# 當前設定：每週一早上 9:00
0 9 * * 1 /Users/geothingsmacbookair/Documents/GitHub/geoBingAn-pdf-sync-tool/run_weekly_sync.sh
```

### 修改排程時間

```bash
# 編輯 crontab
crontab -e

# Cron 格式：分 時 日 月 星期
# 範例：每週三下午 3:00
0 15 * * 3 /path/to/run_weekly_sync.sh

# 範例：每天早上 8:00
0 8 * * * /path/to/run_weekly_sync.sh
```

---

## 🔔 通知設定

### LINE Notify（推薦）

1. 前往 [LINE Notify](https://notify-bot.line.me/) 申請 Token
2. 在 `.env` 中設定：
   ```bash
   LINE_NOTIFY_TOKEN=your-token-here
   ```
3. 執行測試：
   ```bash
   python3 notify.py
   ```

### macOS 系統通知

預設啟用，可在 `.env` 中關閉：
```bash
ENABLE_MACOS_NOTIFY=false
```

### 通知觸發時機

| 事件 | 通知內容 |
|------|----------|
| 執行成功 | 同步數量、上傳數量、執行時間 |
| 執行失敗 | 錯誤類型、錯誤訊息 |

---

## 📁 專案結構

```
geoBingAn-pdf-sync-tool/
├── sync_permits.py                    # 核心：同步 PDF from 台北市政府
├── upload_pdfs.py                     # 核心：上傳 PDF to Backend API
├── generate_permit_tracking_report.py # 核心：生成建案追蹤報告
├── run_weekly_sync.sh                 # 核心：自動執行腳本
│
├── config.py                    # 設定載入（從 .env 讀取）
├── filename_date_parser.py      # 檔名日期解析模組（獨立可測試）
├── jwt_auth.py                  # JWT Token 管理模組（thread-safe）
├── notify.py                    # 通知模組（LINE / macOS）
├── sync_status.py               # 狀態追蹤模組
├── record_sync_result.py        # 執行結果記錄
│
├── .env                         # 環境變數（需自行建立，勿提交）
├── .env.example                 # 環境變數範本
├── credentials.json             # Google Drive 金鑰（需自行建立）
├── requirements.txt             # Python 依賴
├── README.md                    # 本文件
├── .gitignore                   # Git 忽略清單
│
├── state/                       # 狀態追蹤
│   ├── sync_permits_progress.json       # 同步進度
│   ├── uploaded_to_geobingan_7days.json # 上傳記錄
│   ├── sync_status.json                 # 執行狀態與歷史
│   ├── permit_tracking_report.html      # 追蹤報告 (HTML)
│   └── permit_tracking.csv              # 追蹤報告 (CSV)
│
├── logs/                        # 執行日誌
│   └── weekly_sync_*.log        # 週期執行日誌
│
├── docs/                        # 技術文檔
│   ├── architecture.md          # 系統架構設計文件
│   ├── API.md                   # API 說明
│   ├── cron_setup_guide.md      # Cron 設定指南
│   ├── troubleshooting.md       # 問題排解指南
│   └── index.html               # 線上追蹤報告
│
└── tests/                       # 自動化測試
    ├── test_parse_date_from_filename.py  # 日期解析測試 (21 cases)
    └── test_jwt_auth.py                  # JWT 管理測試 (20 cases)
```

---

## 🔍 狀態追蹤

### `state/sync_permits_progress.json`
記錄已同步的建案（從台北市政府）
```json
{
  "processed": ["112建字第0001號", "113建字第0008號", ...],
  "errors": [],
  "restricted": []
}
```

### `state/uploaded_to_geobingan_7days.json`
記錄最近 7 天上傳的 PDF
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

### `state/sync_status.json`
執行狀態追蹤與歷史記錄
```json
{
  "last_run": "2026-03-09T12:00:00",
  "last_status": "success",
  "stats": {
    "total_runs": 52,
    "success_count": 50,
    "failure_count": 2,
    "total_synced_pdfs": 1234,
    "total_uploaded_pdfs": 567
  },
  "history": [
    {
      "date": "2026-03-09",
      "status": "success",
      "synced": 45,
      "uploaded": 12,
      "duration_seconds": 3600
    }
  ]
}
```

### 重置狀態

```bash
# 清除上傳記錄（重新上傳最近 7 天的檔案）
rm state/uploaded_to_geobingan_7days.json

# 清除同步記錄（重新同步所有建案）
rm state/sync_permits_progress.json

# 清除所有狀態
rm state/*.json
```

---

## 🔧 故障排除

詳細問題排解請參考：**[docs/troubleshooting.md](docs/troubleshooting.md)**

### 常見問題快速解答

| 問題 | 解決方案 |
|------|----------|
| 腳本卡住不動 | 死鎖問題，已在 v2.1.0 修復 |
| 401 Unauthorized | JWT Token 過期，會自動刷新 |
| 500 Server Error | 檢查 API 端點是否正確 |
| 504 Timeout | 大型 PDF，增加 timeout 設定 |

### 1. Google Drive 認證失敗

```
❌ 找不到 Service Account 金鑰
```

**解決：**
```bash
# 確認檔案存在
ls -la credentials.json

# 確認 JSON 格式正確
python3 -m json.tool credentials.json
```

### 2. JWT Token 過期

```
❌ 401 Unauthorized - Token has expired
```

**解決：**
- v2.1.0 已內建自動刷新機制
- 確認 `.env` 中有設定 `REFRESH_TOKEN`
- Refresh Token 有效期 7 天，過期需重新登入取得

### 3. PDF 上傳失敗

可能原因：
- API 速率限制 → 增加 `DELAY_BETWEEN_UPLOADS`
- PDF 格式不支援 → 檢查 Backend 日誌
- 認證過期 → Token 會自動刷新

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
with open('state/uploaded_to_geobingan_7days.json') as f:
    data = json.load(f)
    print(f'已上傳: {len(data[\"uploaded_files\"])}')
    print(f'失敗: {len(data[\"errors\"])}')
"
```

### 查看最新日誌

```bash
# 查看最新週期執行日誌
tail -100 logs/weekly_sync_*.log | tail -100

# 即時監控
tail -f logs/weekly_sync_*.log
```

---

## 🔐 安全性

### 敏感檔案管理

**不要提交到 Git：**
- ❌ `.env` - 環境變數（含 Token 等敏感資訊）
- ❌ `credentials.json` - Service Account 金鑰
- ❌ `state/*.json` - 可能包含敏感資訊
- ❌ `*.log` - 執行日誌

這些檔案已在 `.gitignore` 中排除。

**安全的檔案（可提交）：**
- ✅ `.env.example` - 環境變數範本（不含實際值）
- ✅ `config.py` - 設定載入器（從 .env 讀取，不含硬編碼值）

### 權限最小化

Service Account 只需要：
- Google Drive API - 讀取權限
- 共享雲端 - 檢視者權限

---

## 📝 版本歷史

### v3.4.0 (2026-04-02)
- ⚡ sync_permits 並行處理（5 thread ThreadPoolExecutor）
- ⚡ Thread-local Drive service（httplib2 非 thread-safe，每 thread 獨立 instance）
- ⚡ 174 建案同步時間：7 分 19 秒 → 2 分 37 秒
- ⚡ State 格式升級為 dict（向後相容舊 list 格式自動轉換）

### v3.3.0 (2026-04-02)
- ⚡ sync_permits 預載入目標檔案樹（逐檔 API 查詢 → 記憶體 set lookup）
- ⚡ 子資料夾 ID 快取（避免重複查詢同一路徑）
- ⚡ 174 建案同步時間：30+ 分鐘 → 7 分 19 秒
- 🔧 預載入 fail-closed：不完整掃描自動回退逐檔 API 查詢

### v3.2.0 (2026-04-02)
- ⚡ PDF 掃描改為批次 Drive 查詢（1000 次 API → ~61 次分頁）
- ⚡ 報告生成 PDF 統計同樣改為批次查詢（497 次 → ~61 次）
- ⚡ 上傳間隔從 2 秒降到 0.5 秒（後端非同步處理）
- 🔧 批次掃描中斷時完整回退到逐資料夾掃描（不使用部分結果）
- 🔧 報告 PDF 統計保留 unique filename 去重（與舊版語意一致）

### v3.1.0 (2026-04-02)
- ✅ 抽取 JWT Token 管理到 `jwt_auth.py` 共用模組（消除 175 行重複）
- ✅ State 檔案跨 process 安全（flock + read-merge-write + atomic replace）
- ✅ 上傳重試機制：503 指數退避（5s/15s/30s），502/504 不重試避免重複
- ✅ Shell 級聯失敗保護：步驟 1/2 失敗自動跳過下游步驟
- ✅ SHARED_DRIVE_ID 統一從 config.py 載入（含向後相容 fallback）
- ✅ 移除已停用的 parallel upload 死碼（~30 行）
- ✅ 修正 exit code：錯誤情境 exit(1)，正常情境 exit(0)
- ✅ 新增 JWT 測試（20 cases），總計 41 tests
- 📄 新增 `docs/architecture.md` 設計文件

### v3.0.0 (2026-04-02)
- ✅ 上傳過濾改為依 PDF 檔名日期（非 Google Drive modifiedTime）
- ✅ 新增 `filename_date_parser.py` 獨立模組，支援 7 種日期格式
- ✅ 完整掃描所有資料夾（不再依賴 DAYS_AGO 截斷）
- ✅ 智慧快取失效機制（偵測資料夾數量變化自動重建）
- ✅ 新增 pytest 自動化測試（21 cases）
- 🔧 修復 4 碼日期誤判問題（如 `1231觀測報告` 不再誤判為 2026-12-31）
- 🔧 修復 `_parsed_date` datetime 物件導致 JSON 序列化失敗

### v2.3.0 (2026-03-09)
- ✅ 新增環境變數管理（`.env` 檔案 + `python-dotenv`）
- ✅ 新增通知模組（LINE Notify / macOS 系統通知）
- ✅ 新增執行狀態追蹤與歷史記錄（`sync_status.py`）
- ✅ 改善 `run_weekly_sync.sh` 錯誤處理（`set -e` + `trap cleanup`）
- ✅ JWT Token 更新現在會正確寫入 `.env` 檔案
- 🔒 敏感資訊從 `config.py` 移至 `.env`（不會被提交到 Git）
- 📄 新增 `docs/automation_improvement_plan.md` 文件

### v2.2.0 (2026-03-09)
- ✅ 新增 `generate_permit_tracking_report.py` 建案追蹤報告功能
- ✅ 更新台北市政府 PDF 來源 URL
- ✅ 修正輸出緩衝問題（加入 flush=True）
- ✅ 完成 522 個 2026/115年 PDF 上傳至究平安
- 🧹 整理 repository：移動測試檔案至 archive/

### v2.1.0 (2026-01-09)
- ✅ 新增 JWT Token 自動刷新機制
- ✅ 修復死鎖問題（threading.Lock 優化）
- ✅ 改用正確的 API 端點 `/api/reports/construction-reports/upload/`
- ✅ 新增問題排解指南 `docs/troubleshooting.md`
- ⚡ 上傳間隔優化：20秒 → 2秒

### v2.0.0 (2026-01-02)
- ♻️ 簡化工具定位：只負責同步和上傳
- ♻️ 移除附件補充功能（由後端處理）
- ♻️ 移除狀態檢查工具（可選功能移到 archive/）
- ✅ 保留核心功能：`sync_permits.py` + `upload_pdfs.py`
- ✅ 定期執行設定：cron job 每週一早上 9:00
- 📄 更新文檔反映正確需求

### v1.1.0 (2026-01-02)
- ✅ 完成自動上傳測試：13 個 Reports 成功建立
- ✅ 新增完整的上傳歷史記錄
- ✅ 改進狀態追蹤機制
- 📊 測試結果：81.25% 成功率（13/16）

### v1.0.0 (2025-12-31)
- ✅ 初始版本
- ✅ 支援從台北市政府同步建案 PDF
- ✅ 支援上傳到 geoBingAn Backend API
- ✅ 狀態追蹤和 7 天時間過濾

---

## 🆘 獲取幫助

### 問題回報

1. **工具問題**：建立 Issue 在此 repository
2. **Backend 問題**：前往 [geoBingAn_v2_backend](https://github.com/GeoThings/geoBingAn_v2_backend)
3. **API 問題**：查看 Backend API 文檔

### 相關連結

- [geoBingAn Backend Repository](https://github.com/GeoThings/geoBingAn_v2_backend)
- [geoBingAn Web App](https://riskmap.tw/)

---

## 📄 授權

MIT License

---

**維護者**: geoBingAn Team
**最後更新**: 2026-04-02
