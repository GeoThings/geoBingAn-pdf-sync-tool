# 系統架構設計文件

> geoBingAn 建案監測同步工具 v5.2 架構說明
> 最後更新：2026-06-09

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

### 自動化排程（macOS launchd）

使用 `launchd` 而非 `cron`：Mac 從睡眠醒來時自動補跑錯過的排程。

| 時間 | LaunchAgent | 內容 |
|------|-------------|------|
| 每日 08:00 | `com.geothings.geobingan.healthcheck` | Token/磁碟/同步狀態/API/launchd job 巡檢（PR #53） |
| 每日 10:00 | `com.geothings.geobingan.weeklysync` | 完整流程（步驟 1-4）+ 週一加步驟 5 產 PDF |
| 週五 17:00 | `com.geothings.geobingan.fridayreport` | 總結週報 PDF → ClickUp |

安裝：`./setup_launchd.sh` · 卸載：`./uninstall_launchd.sh` · plist 位於 `launchd/`

> ✅ **2026-06-09 ROOT CAUSE 確認 + RESOLVED**：18 天 launchd 自動觸發系統性 fail 的 root cause = repo 位於 `~/Documents/` 被 iCloud Drive `FileProvider` 接管。fix = 把 repo 搬到 `~/Developer/`（FileProvider 域外）。搬完後第一次 launchd kickstart healthcheck = exit 0、echo 寫入。詳見下方「Auto-trigger 失敗 root cause」段。

#### Wake-from-sleep 排程行為（2026-05 incident RCA）

筆電型 Mac 長期睡眠會讓 `StartCalendarInterval` 完全跳過排程時間（launchd 不會主動喚醒系統）。修復走兩層：

1. **`pmset repeat wakepoweron MTWRFSU 07:55:00`** — 每天 07:55 把系統喚醒，讓 launchd 排程能準時觸發。系統指令、不在 repo plist 內、需手動 `sudo pmset` 安裝。
2. **healthcheck plist 加 `sleep 30 &&` + diagnostic echo**（PR #47）— 喚醒到 launchd spawn 之間的 race window 暫時緩衝。

#### Auto-trigger 失敗 root cause（2026-05~2026-06-09 已 RESOLVED）

##### 現象

5/22 起連續 18 天觀察，三個 LaunchAgent 自動觸發**系統性失敗**：

| Job | launchd auto-trigger（舊位置） | 手動 `./script.sh` |
|---|---|---|
| healthcheck（每日 08:00） | runs +1、exit=1、echo **沒寫入** logs/health_check.log | exit 0、echo 正常寫入 |
| weeklysync（每日 10:00、原 09:00） | runs +1、exit=78 EX_CONFIG、trigger marker **沒寫入** | exit 0、PDF 流程完整跑完 |
| fridayreport（週五 17:00、原 18:00） | runs +1、exit=78、trigger marker **沒寫入** | exit 0、週報 PDF 上傳 ClickUp |

共通指紋（所有自動 fail case）：

1. `launchctl print` runs 計數 +1（launchd 確實「嘗試」）、但 last exit code != 0
2. `launchd_*_err.log` size = **0 bytes**（stderr 完全沒寫入 = script 根本沒跑到第一行）
3. trigger marker / echo 沒寫入對應 log（script 第一行就是 marker，沒寫入 = 失敗發生在 launchd-spawn 階段、bash 沒接管）
4. 同樣 plist、同樣 user 環境，user shell 直接跑 script = 100% 成功

##### ✅ 確認的 root cause：iCloud Drive FileProvider 域

**repo 位於 `~/Documents/GitHub/geoBingAn-pdf-sync-tool/`、被 iCloud Drive 的 `com.apple.CloudDocs` FileProvider 接管**。FileProvider 對 launchd-spawn 出來的 child process 偶發性拒絕檔案讀取，造成：

- bash spawn 時 fork → 載入 script 的 read = `Operation timed out / canceled`
- 沒寫到 script 第一行 echo
- exit 1 或 78 EX_CONFIG（launchd 視為 config error）
- stderr 0 bytes（因為 bash 連起來都沒起來）

**6/09 觸發 root cause 確認的 sequence**：

1. 早上 manual `./run_weekly_sync.sh` 跑步驟 2.5/3/4 時 Python `<frozen site>` import 開始大量 `TimeoutError`
2. `cp docs/index.html` 與 `git commit` 全部 timeout
3. 排查發現 `cat .git/HEAD` 也 `Operation timed out`
4. `brctl status` 顯示 `com.apple.CloudDocs[1] foreground ... last-sync:2026-06-09 10:38:53.334`
5. 跟 manual sync timeout 時間 `10:38:56` 對齊 **3 秒**
6. `xattr ~/Documents` → `com.apple.file-provider-domain-id = com.apple.CloudDocs.iCloudDriveFileProvider/...` 確認 Documents 在 iCloud Drive 域

**Fix（已執行）**：把 repo 搬到 `~/Developer/geoBingAn-pdf-sync-tool/`（不在 iCloud Drive 任何 root 下），更新 launchd plist 三條絕對路徑，bootout + bootstrap。搬完第一次 launchd kickstart healthcheck = **runs 0→1、exit=0、echo 正常寫入**，18 天內第一次成功。

##### 已推翻的 hypothesis（誠實記錄）

下列 4 個 hypothesis 都做過對照實驗、全部推翻——但**它們之所以全錯，是因為都沒考慮 iCloud FileProvider 這條變數**：

1. **❌ macOS 15 `~/Documents` TCC 保護擋 launchd spawn**
   - 4 個對照 experiment（home root / Documents 路徑 / 真實 repo 深處 + cd + mkdir + 寫入 / venv activate + python3.14）全部 spawn 成功 exit 0
   - 真相：TCC 沒問題、但 experiment script 是當下手寫的「新檔」、iCloud 還沒接管所以沒踩 FileProvider 拒絕；real weeklysync.sh 是長期存在的 iCloud-managed 檔

2. **❌ 距 pmset wake event 的時間是關鍵變數**
   - 6/01 09:00 weeklysync 距 wake 65 分鐘也 fail
   - 真相：時間不是變數、iCloud sync 隨時可能 race 鎖住 FileProvider read

3. **❌ 「user 在用 Mac 才會成功」（user context 假設）**
   - 6/03 10:30 手動 `launchctl kickstart` 真 weeklysync job 也 exit 78
   - 真相：kickstart 走 launchd-internal spawn path = iCloud 還是會卡；user shell 跑成功是因為 shell 已經 warm、檔案 cache 命中、繞過 FileProvider 重檢

4. **❌ 改 schedule 從 09:00 → 10:00（PR #56）讓系統多 settle 1 小時就會通過**
   - 6/04、6/08、6/09 10:00 都 fired 但 exit 78
   - 真相：iCloud 8:00–10:00 都活躍、settle 沒用

##### 為何 manual shell 大多成功、偶爾也 fail？

manual shell 跑 = user shell 已經把 bash / venv / script 載入記憶體；FileProvider 只在 cold-start read 時 throttle。但 6/09 manual sync 跑了 37 分鐘後 fail 出現了 — 那是 iCloud 在 sync 中觸發整個 Documents tree 的 FileProvider verification、即使 warm process 也被卡。所以「manual 100% 成功」其實是觀察偏差、不是事實。

##### launchd backoff silent permanent lock（systematic 不是偶發）

EX_CONFIG 78 觸發 launchd 內部 backoff **silent permanent lock**，後續 schedule 完全不再 spawn（連嘗試都沒、runs 計數不再 +1）。兩週內 3 個 job 都踩過（PR #47 healthcheck / PR #49 fridayreport / PR #51 weeklysync），無 alerting、需下游發現「咦週報沒進來」才察覺。fridayreport 鎖 3 週、weeklysync 鎖 4 週才被人手動 spot。**這條 backoff 行為跟 iCloud root cause 是獨立的、修了 root cause 之後仍然要小心 backoff 鎖死**。

**清除方式**：

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.geothings.geobingan.X.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.geothings.geobingan.X.plist
```

**Ops discipline（每兩週手動巡檢）**：

```bash
for j in healthcheck weeklysync fridayreport; do
  echo "=== $j ==="
  launchctl print "gui/$(id -u)/com.geothings.geobingan.$j" 2>/dev/null \
    | grep -E "runs|last exit|state"
done
```

**自動兜底（PR #53）**：`health_check.py` 加 `check_launchd_jobs()`，每日 08:00 healthcheck 跑時掃三個 job、發現 `last exit != 0` 寫進 ClickUp 通知。6/02 首次真實救援——把原本要等 4 週才被發現的 weeklysync 鎖死提早到 1 天浮現。

##### Diagnostic marker 兩種模式（PR #47 / #49 / #51）

無 stderr、無 log 的 fail 唯一線索就是「script 第一行有沒有跑到」。兩種 plist shape 對應兩種 marker 位置：

| Plist 形式 | Marker 位置 | 範例 job |
|---|---|---|
| `bash -c '...'` 包裝型 | echo 寫在 plist 命令字串裡 | healthcheck |
| 直接呼叫 script 型 | marker 寫在 script 最早可能位置（第 10-15 行內、setup `set -e` 之前） | weeklysync / fridayreport |

兩者目的相同：marker 寫入 = launchd spawn 成功、後續錯誤可往下排查；marker 沒寫入 = 確認 launchd-spawn-level fail、無法繼續排查（接受 workaround）。

##### Follow-up backlog（updated 2026-06-09）

- [x] ~~連續觀察 healthcheck.log 量化 wake-to-spawn delay~~ — **撤銷**：原假設前提（成功時的 delay 分佈）不存在；root cause 是 iCloud FileProvider、跟 wake delay 無關
- [x] ~~移除 / 升級 / 保留 `sleep 30` 三選一~~ — **保留**：`sleep 30` 在新位置可能依然冗餘、但移除回報太低、留著當 defense in depth
- [x] ~~維持每日手動觸發 + PR #53 兜底~~ — **解除**：repo 搬家後 launchd 自動觸發應穩定、回到原設計
- [ ] 6/09 後 7 天觀察期：每日驗證 weeklysync / healthcheck runs 計數 + last exit；連續 5 天 exit 0 後關閉觀察
- [ ] 若 macOS 升級或 user 重啟 iCloud Drive Documents 同步、要記得驗證 repo 不被重新接管（`xattr ~/Developer/geoBingAn-pdf-sync-tool` 應該無 `file-provider-domain-id`）

##### 「不要把任何 macOS 開發工具放在 ~/Documents/」(general principle)

iCloud Drive `Desktop & Documents` 同步是 macOS 預設開啟的、會把 `~/Documents` 整棵樹接管成 FileProvider domain。任何放在裡面的 git repo / venv / 大量小檔案，都會踩到本案的 silent FileProvider throttle。**正確位置 = `~/Developer/` / `~/Code/` / `~/src/` 等不在 iCloud sync root 下的目錄**。

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
│   ├── permit_utils.py                 ← normalize_permit + 名稱提取
│   ├── drive_utils.py                  ← 共用 Drive 掃描
│   └── requests.Session                ← API 連線池（TCP/TLS 重用）
│
├── generate_permit_tracking_report.py（資料收集 + main）
│   ├── config.py → .env
│   ├── jwt_auth.py
│   ├── permit_utils.py                 ← normalize_permit
│   ├── drive_utils.py                  ← 共用 Drive 掃描
│   ├── report_template.py             ← HTML/CSV 報告模板
│   └── requests.Session                ← API 連線池
│
└── generate_weekly_report.py
    ├── state/permit_registry.json
    └── Chrome headless（PDF 渲染）
```

### 獨立可測試模組（零外部服務依賴）

| 模組 | 職責 | 測試 |
|------|------|------|
| `permit_utils.py` | normalize_permit + 檔名名稱提取（30+ 預編譯 regex） | 16+13 cases |
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
| `weekly_snapshots/{date}.json` | sync 後狀態快照（供 compute_diff 算趨勢） | 每次 sync | 否（local-only，見下） |

### Weekly snapshots：local-only state（PR #45）

`weekly_snapshots/` 是純粹的 sync-to-sync diff 工具：
- `get_previous_snapshot()` 只讀**最近 1 筆**非今天的快照
- `compute_diff(curr, None)` 三重容錯 — fresh clone 第一次 sync 無 trend 輸出、之後正常
- 月度趨勢（`check_monthly_activity_trend`）走 `uploaded_to_geobingan_7days.json`，**不依賴 snapshots**
- 真正的歷史歸檔在 ClickUp（每週 sync 自動上傳 PDF）

→ 不進 git。每位執行者本地各自維護自己的 snapshot 序列。

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

所有設定值（`SHARED_DRIVE_ID`、`DAYS_AGO`、`MAX_UPLOADS`、`DELAY_BETWEEN_UPLOADS`、`CLICKUP_TOKEN`）統一由 `config.py` 從 `.env` 載入，各腳本 import 使用，不再有本地硬編碼覆蓋。多城市配置由 `city_config.py` 從 `cities.json` 載入，空白欄位自動回退到 `.env` 預設值。

## 測試策略

| 測試檔案 | 覆蓋模組 | Cases | 依賴 |
|----------|----------|-------|------|
| `test_parse_date_from_filename.py` | filename_date_parser.py | 21 | 無 |
| `test_jwt_auth.py` | jwt_auth.py | 14 | unittest.mock |
| `test_normalize_permit.py` | permit_utils.normalize_permit | 13 | 無 |
| `test_extract_name.py` | permit_utils.extract_name_from_filename | 16 | 無 |
| `test_config.py` | config.escape_drive_query | 7 | 無 |
| `test_drive_utils.py` | drive_utils.build_folder_resolver | 8 | 無 |
| `test_report_template.py` | report_template (HTML + CSV + XSS) | 11 | tempfile |
| `test_csv_import.py` | sync_permits.load_csv_list | 8 | tempfile |
| **合計** | | **63** | |

設計原則：
- 所有測試 import 零依賴模組（`permit_utils`、`drive_utils`），不觸發 credentials 或 Google API（lazy init）
- 可在 CI（無 credentials.json）或乾淨環境執行
- Smoke tests 覆蓋報告生成端到端路徑（含 XSS escaping、file round-trip）
- `normalize_permit` 和 `extract_name_from_filename` 統一在 `permit_utils.py`，消除 test import side effects
