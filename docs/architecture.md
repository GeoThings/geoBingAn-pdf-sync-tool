# 系統架構設計文件

> geoBingAn 建案監測同步工具 v5.1 架構說明
> 最後更新：2026-04-16

## 系統概覽

```
┌─────────────────┐
│  各縣市政府       │──┐
│  PDF 列表         │  │
└─────────────────┘  │  ┌──────────────┐     ┌─────────────────┐
                     ├─▶│ Google Drive  │────▶│  riskmap.today  │
┌─────────────────┐  │  │ Shared Drive │     │  (geoBingAn API)│
│  CSV 匯入         │──┘  └──────────────┘     └─────────────────┘
│  (NGO 手動整理)   │   sync_permits.py         upload_pdfs.py
└─────────────────┘       步驟 1                  步驟 2

                    ┌──────────────────┐     ┌──────────────┐
                    │ 追蹤報告 HTML/CSV │────▶│ GitHub Pages │
                    └──────────────────┘     └──────────────┘
               generate_permit_tracking_report.py
                         步驟 3                  步驟 4

                    ┌──────────────────┐     ┌──────────────┐
                    │  週報 PDF         │────▶│   ClickUp    │
                    └──────────────────┘     └──────────────┘
               generate_weekly_report.py
                         步驟 5
```

### 自動化排程

| 時間 | 觸發腳本 | 內容 |
|------|----------|------|
| 每日 08:00 | `health_check.py --notify` | Token/磁碟/同步狀態/API 檢查 |
| 週一 09:00 | `run_weekly_sync.sh` | 完整流程（步驟 1-7） |
| 週五 18:00 | `run_friday_report.sh` | 總結週報 PDF → ClickUp |

## 模組依賴關係

```
run_weekly_sync.sh（orchestrator）
├── sync_permits.py
│   ├── city_config.py → cities.json    ← 多城市配置
│   ├── config.py → .env
│   └── drive_utils.py                  ← 共用 Drive 掃描
│
├── upload_pdfs.py
│   ├── config.py → .env
│   ├── jwt_auth.py                     ← 共用 JWT 管理（auto-rotate refresh token）
│   └── filename_date_parser.py         ← 共用日期解析
│
├── match_permits.py
│   ├── config.py → .env
│   ├── jwt_auth.py
│   ├── drive_utils.py                  ← 共用 Drive 掃描
│   └── permit_utils.py                 ← 共用建案名稱提取
│
├── generate_permit_tracking_report.py（907 行，資料收集 + main）
│   ├── config.py → .env
│   ├── jwt_auth.py
│   ├── drive_utils.py                  ← 共用 Drive 掃描
│   ├── permit_utils.py                 ← 共用建案名稱提取
│   └── report_template.py             ← HTML/CSV 報告模板（656 行）
│
└── generate_weekly_report.py
    ├── state/permit_registry.json
    └── Chrome headless（PDF 渲染）
```

### 獨立可測試模組（零外部服務依賴）

| 模組 | 職責 | 測試 |
|------|------|------|
| `permit_utils.py` | 從 PDF 檔名提取建案名稱（30+ regex） | 16 cases |
| `filename_date_parser.py` | 從 PDF 檔名解析日期（7 種格式） | 21 cases |
| `jwt_auth.py` | JWT decode/expire/refresh（thread-safe） | 14 cases |
| `drive_utils.py` | 共用 Drive 掃描（list folders, resolve subfolder hierarchy） | 8 cases |
| `report_template.py` | HTML/CSV 報告生成 | 11 cases |
| `config.py` | 配置 + escape_drive_query | 7 cases |
| `city_config.py` | 多城市配置載入/解析 | — |

設計原則：這些模組不依賴 `credentials.json` 或任何外部服務（lazy init），可在 CI 或乾淨環境直接 import 和測試。

### CI Pipeline

```
GitHub Actions → pytest tests/ → Python 3.11 + 3.12 → 63 tests
觸發條件：push to main / PR to main
```

## 資料流

### 步驟 1：sync_permits.py

```
cities.json（多城市配置）
    │
    ▼ 依 source_type 分流
    │
    ├── PDF: 下載政府 PDF → 智慧分塊解析
    └── CSV: 載入本地 CSV（NGO 手動整理）
    │
    ▼
N 個建案（含 Google Drive 連結）
    │
    ▼ 比對 state/sync_permits_progress.json
未處理建案
    │
    ▼ ThreadPoolExecutor（5 並行，thread-local Drive service）
    │
    ├── 每個建案：預載入目標檔案樹到記憶體 set
    │   （fail-closed：不完整掃描回退逐檔 API）
    │
    ├── 來源檔案 vs 目標 set 比對（O(1) lookup，零 API）
    │
    └── 只複製新檔（子資料夾 ID 快取）
    │
state/sync_permits_progress.json 更新（thread-safe _state_lock）
```

**Thread safety 設計：**
- `credentials`：共用（thread-safe）
- `httplib2.Http`：每 thread 獨立（`threading.local()` + `get_thread_drive_service()`）
- `state` 寫入：`_state_lock` 保護
- 輸出：`_print_lock` 保護
- 快取：`_target_file_cache` 每建案獨立 key，`_subfolder_cache` 跨建案共享但寫入不衝突（不同路徑）

### 步驟 2：upload_pdfs.py

```
Shared Drive（批次查詢，~12 次分頁 API 呼叫）
    │
    ▼ 一次取得所有 PDF（~11,000 個）+ folder lookup table 對應資料夾
    │  （失敗時回退到逐資料夾查詢，丟棄部分結果確保完整性）
    │
    ▼ filename_date_parser 解析檔名日期
    │
    ▼ 過濾：日期 > 2026-02-17（農曆新年）
~56 個符合條件的 PDF
    │
    ▼ 排除已上傳（state/uploaded_to_geobingan_7days.json）
    │
    ▼ 逐一下載 + 上傳到 riskmap.today API（0.5 秒間隔）
    │
    ▼ 成功立即寫入 state（flock + merge）
```

### 步驟 2.5：match_permits.py（建案名稱交叉比對）

```
6 個資料來源交叉比對：
├── 1. 台北市政府 PDF（建照清單 + 來源資料夾名稱）
├── 2. Google Drive 來源資料夾名稱
├── 3. Google Drive PDF 檔名（含子資料夾遞迴掃描，26,820 個 PDF）
├── 4. riskmap.today API construction-projects（580 個，去重 + 滑動視窗匹配）
├── 5. riskmap.today API construction-reports
└── 6. riskmap.today API construction-alerts（即時警戒值）
    │
    ▼ 名稱清理（extract_name_from_filename，支援括號格式）
    │
    ▼ 通用名稱過濾（監測、監測報告、工地監測數據等不用於匹配）
    │
    ▼ 優先順序合併（手動確認 > alert_csv > api_match > drive_pdf > source_folder）
    │
    ▼ 名稱優化（API 名稱自動取代通用/短名稱）
    │
    ▼ 產出 state/permit_registry.json
        411 筆建案，378 筆有名稱（92%），66 筆有即時警戒

手動確認：31 筆建案名稱已由使用者逐一確認
API project 匹配：116 筆（滑動視窗 + 去重）
```

### 步驟 3：generate_permit_tracking_report.py

```
資料來源（4 路合併）：
├── Google Drive（批次查詢 + 子資料夾遞迴 + unique filename 去重）
├── riskmap.today API（19,000+ 筆報告，名稱模糊匹配 15,002 筆對應）
├── 台北市政府 PDF（建照清單 + 來源資料夾名稱）
└── state/permit_registry.json（建案名稱 + 即時警戒值）
    │
    ▼ 合併 + html.escape() + 名稱載入（6 來源交叉比對，92% 覆蓋）
    │
    ▼ 已結案標記（建照年份 ≤ 110 年且無系統報告）
    │
    ▼ 預設排序：最近更新排前面
    │
    ▼ 產出究心黑紅品牌 HTML 報告 + CSV

建案名稱來源（permit_registry.json 優先順序）：
  1. 手動確認（31 筆）
  2. construction-alerts API（即時警戒值，16 筆）
  3. construction-projects / construction-reports API
  4. Drive PDF 檔名（最大來源，288 筆）
  5. 來源 Drive 資料夾名稱（65 筆）

報告功能：
├── 「需要處理」儀表板（預設折疊，可捲動）
├── 動態日期計算（瀏覽器端 JS）
├── 搜尋（建照號碼 + 工地名稱，250ms debounce）
├── Segmented Control 篩選 + aria-pressed 無障礙
├── 排序箭頭（↑↓ 紅色指示器）
├── 狀態 badge 圖示（✔⏳⬆🏁──✖）
├── 擴大觸控熱區、nowrap 防斷行、空狀態提示
└── 水平捲動（手機不隱藏欄位）
```

**待開發：** 後端 API `report_category_name` 欄位 — AI 解析出的 `projectMetadata.projectName` 存在後端 DB（ConstructionSite.name），但 construction-reports API 未回傳此欄位。已建立 ClickUp task（[#86ex8c82c](https://app.clickup.com/t/86ex8c82c)），待後端在 `ConstructionReportListSerializer` 加入 `report_category_name`，即可實現 100% 自動化名稱覆蓋。

## State 管理

### 狀態檔案

| 檔案 | 用途 | 寫入頻率 | Git 追蹤 |
|------|------|----------|----------|
| `upload_history_all.json` | 永久上傳歷史（防重複上傳） | 每次成功上傳 | ✅ 是 |
| `permit_registry.json` | 建案名稱交叉比對結果（6 來源） | match_permits.py 執行時 | ✅ 是 |
| `sync_permits_progress.json` | 已處理建案清單 | 每個建案 | 否 |
| `uploaded_to_geobingan_7days.json` | 已上傳 PDF + 快取 | 每次成功上傳 | 否 |
| `sync_status.json` | 執行狀態與歷史 | 每次執行 | 否 |

### 上傳歷史持久化（v3.7+）

`upload_history_all.json` 提交到 git，`load_state()` 啟動時自動合併：
```
load_state()
    ├── 讀取本地 state（uploaded_to_geobingan_7days.json，可能不存在）
    ├── 讀取 git 追蹤歷史（upload_history_all.json，始終存在）
    └── uploaded_files = 本地 ∪ 歷史（set union，不重複上傳）
```

### 跨 Process 安全寫入（v3.1+）

```
save_state(state)
    │
    ▼ fcntl.flock(LOCK_EX)     ← 排他鎖，阻擋其他 process
    │
    ▼ 讀取磁碟最新 state
    │
    ▼ merge uploaded_files（聯集）
    ▼ merge errors（去重）
    │
    ▼ 寫入 .tmp.{PID}          ← 每個 process 獨立暫存檔
    │
    ▼ os.replace()             ← POSIX 原子操作
    │
    ▼ fcntl.flock(LOCK_UN)     ← 釋放鎖
```

設計決策：
- **成功上傳立即寫入**：確保 crash 後不會重複上傳（冪等性）
- **錯誤記錄批次寫入**：每 10 次，遺失不影響上傳/跳過判斷
- **flock + merge**：多個 process 重疊執行時不會遺失對方的寫入

## 錯誤處理

### Shell 級聯保護（v3.1+）

```
run_weekly_sync.sh
│
├── 步驟 1 失敗 → 跳過步驟 2, 3（依賴同步資料）
│                  步驟 4 仍執行（推送現有報告）
│
├── 步驟 2 失敗 → 跳過步驟 3（報告會不完整）
│                  步驟 4 仍執行
│
└── 步驟 3 失敗 → 步驟 4 仍執行
```

### 上傳重試策略

| HTTP Status | 行為 | 原因 |
|-------------|------|------|
| 200/201/202 | 成功 | — |
| 401 | 刷新 Token 後重試一次 | Token 過期 |
| 502/504 | 不重試，回傳 processing | PDF 可能已送達後端 |
| 503 | 指數退避重試（5s/15s/30s） | 伺服器暫時不可用 |
| Connection timeout | 指數退避重試 | 網路層逾時 |

### Exit Code 語意

| 情境 | Exit Code | Shell 偵測 |
|------|-----------|-----------|
| 掃描失敗 / 無資料夾 / 無 PDF | 1 | `if !` 觸發 handle_error |
| 全部已上傳 / 使用者取消 | 0 | 正常結束 |
| 上傳完成（有成功有失敗） | 0 | 正常結束 |
| 未預期例外 | 1 | `if !` 觸發 handle_error |

## 檔名日期解析

### 支援格式（filename_date_parser.py）

| 模式 | 範例 | 解析結果 |
|------|------|----------|
| 西元年連字號 | `2026-02-23` | 2026-02-23 |
| 西元年8碼 | `20260303` | 2026-03-03 |
| 民國年中文 | `115年03月09日` | 2026-03-09 |
| 民國年點分隔 | `115.03.24` | 2026-03-24 |
| 民國年7碼（分隔） | `_1150311_` | 2026-03-11 |
| 民國年7碼（嵌入） | `連雲玥恒1150331報告` | 2026-03-31 |
| 短日期+路徑年份 | `2026/...0303觀測報告` | 2026-03-03 |

設計決策：
- 無法解析日期的檔案**跳過**（不上傳），避免誤判
- 4碼短日期**必須**有路徑年份上下文，否則跳過
- cutoff 使用 `>` 不含當天（農曆新年初一之後）

## JWT Token 管理

### jwt_auth.py 架構

```python
get_valid_token(current_token, refresh_token, refresh_url)
    → (valid_token, was_refreshed, new_refresh_token)
```

- **Thread-safe**：`_token_lock` 保護整個 check-and-refresh 流程
- **自動刷新**：過期前 5 分鐘（buffer_seconds=300）觸發
- **Refresh Token 自動輪替**：API 回傳新 refresh_token 時自動寫回 .env
- **降級策略**：刷新失敗時回傳舊 Token 嘗試（可能失敗但不中斷流程）
- **雙格式支援**：API 回應 `access`/`access_token` + `refresh`/`refresh_token` 都接受
- **所有呼叫者都持久化**：upload_pdfs、match_permits、report generator 刷新時都寫回 .env

## 設定管理

### 優先順序

```
config.py (from .env)  →  環境變數  →  硬編碼預設值
```

所有三個腳本的 `SHARED_DRIVE_ID` 都遵循此 fallback 策略，確保舊 config.py（不含新欄位）不會導致 import 失敗。

## 測試策略

| 測試檔案 | 覆蓋模組 | Cases | 依賴 |
|----------|----------|-------|------|
| `test_parse_date_from_filename.py` | filename_date_parser.py | 21 | 無 |
| `test_jwt_auth.py` | jwt_auth.py | 14 | unittest.mock |
| `test_normalize_permit.py` | match_permits.normalize_permit | 13 | 無 |
| `test_extract_name.py` | permit_utils.extract_name_from_filename | 16 | 無 |
| `test_config.py` | config.escape_drive_query | 7 | 無 |
| `test_drive_utils.py` | drive_utils.build_folder_resolver | 8 | 無 |
| `test_report_template.py` | report_template (HTML + CSV) | 11 | tempfile |
| `test_csv_import.py` | sync_permits.load_csv_list | 8 | tempfile |
| **合計** | | **63** | |

設計原則：
- 測試只 import 獨立模組，不觸發 credentials 載入或 Google API 初始化（lazy init）
- 可在 CI（無 credentials.json）或乾淨環境執行
- Smoke tests 覆蓋報告生成端到端路徑（包含 XSS escaping 驗證）
