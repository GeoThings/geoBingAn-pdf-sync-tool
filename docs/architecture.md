# 系統架構設計文件

> geoBingAn PDF 同步上傳工具 v3.1 架構說明

## 系統概覽

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────────┐
│  台北市政府       │────▶│ Google Drive  │────▶│  riskmap.today  │
│  建管處 PDF 列表  │     │ Shared Drive │     │  (geoBingAn API)│
└─────────────────┘     └──────────────┘     └─────────────────┘
     sync_permits.py         upload_pdfs.py
         步驟 1                  步驟 2

                    ┌──────────────────┐     ┌──────────────┐
                    │ 追蹤報告 HTML/CSV │────▶│ GitHub Pages │
                    └──────────────────┘     └──────────────┘
               generate_permit_tracking_report.py
                         步驟 3                  步驟 4
```

## 模組依賴關係

```
run_weekly_sync.sh（orchestrator）
├── sync_permits.py
│   └── config.py → .env
│
├── upload_pdfs.py
│   ├── config.py → .env
│   ├── jwt_auth.py          ← 共用 JWT 管理
│   └── filename_date_parser.py  ← 共用日期解析
│
└── generate_permit_tracking_report.py
    ├── config.py → .env
    └── jwt_auth.py          ← 共用 JWT 管理
```

### 獨立可測試模組

| 模組 | 職責 | 外部依賴 | 測試 |
|------|------|----------|------|
| `filename_date_parser.py` | 從 PDF 檔名解析日期 | 無 | 21 cases |
| `jwt_auth.py` | JWT decode/expire/refresh | `requests` | 20 cases |

設計原則：這兩個模組不依賴 `config.py`、Google API 或任何認證，可在任何環境直接 import 和測試。

## 資料流

### 步驟 1：sync_permits.py

```
台北市政府 PDF 列表
    │
    ▼ 下載 + 解析
292 個建案（含 Google Drive 連結）
    │
    ▼ 比對 state/sync_permits_progress.json
未處理建案
    │
    ▼ 逐一掃描來源 Drive，複製新檔到 Shared Drive
state/sync_permits_progress.json 更新
```

### 步驟 2：upload_pdfs.py

```
Shared Drive 全部 1000 個資料夾
    │
    ▼ 掃描所有 PDF（~11,000 個）
    │
    ▼ filename_date_parser 解析檔名日期
    │
    ▼ 過濾：日期 > 2026-02-17（農曆新年）
~56 個符合條件的 PDF
    │
    ▼ 排除已上傳（state/uploaded_to_geobingan_7days.json）
    │
    ▼ 逐一下載 + 上傳到 riskmap.today API
    │
    ▼ 成功立即寫入 state（flock + merge）
```

### 步驟 3：generate_permit_tracking_report.py

```
Google Drive 資料夾 + riskmap.today API + 台北市政府 PDF
    │
    ▼ 合併三方資料
    │
    ▼ 產出 HTML 互動式報告 + CSV
state/permit_tracking_report.html
state/permit_tracking.csv
```

## State 管理

### 狀態檔案

| 檔案 | 用途 | 寫入頻率 |
|------|------|----------|
| `sync_permits_progress.json` | 已處理建案清單 | 每個建案 |
| `uploaded_to_geobingan_7days.json` | 已上傳 PDF + 快取 | 每次成功上傳 |
| `upload_history_all.json` | 永久上傳歷史 | 每次成功上傳 |
| `sync_status.json` | 執行狀態與歷史 | 每次執行 |

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
    → (valid_token, was_refreshed)
```

- **Thread-safe**：`_token_lock` 保護整個 check-and-refresh 流程
- **自動刷新**：過期前 5 分鐘（buffer_seconds=300）觸發
- **降級策略**：刷新失敗時回傳舊 Token 嘗試（可能失敗但不中斷流程）
- **雙格式支援**：API 回應 `access` 或 `access_token` 都接受

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
| `test_jwt_auth.py` | jwt_auth.py | 20 | unittest.mock |

設計原則：測試只 import 獨立模組，不觸發 config.py / Google API 初始化，可在 CI 或乾淨環境執行。
