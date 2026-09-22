# 系統架構設計文件

> geoBingAn 建案監測同步工具 v5.3 架構說明
> 最後更新：2026-09-15（新增：清單動態抓取、告警送達、解析預算守門）

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

使用 `launchd` 而非 `cron`。**注意：`StartCalendarInterval` 不會主動喚醒 Mac，睡眠期間錯過的排程也不保證補跑**，因此必須搭配 `sudo pmset repeat wakepoweron MTWRFSU 07:55:00`（詳見下方 wake-from-sleep 段）。

| 時間 | LaunchAgent | 內容 |
|------|-------------|------|
| 每日 08:00 | `com.geothings.geobingan.healthcheck` | 10 項巡檢：Token／磁碟／同步狀態／API／launchd job（PR #53）／上傳暫停（#57）／解析積壓／解析預算（PR #80）／清單新鮮度／來源資料夾失效（PR #82）；異常經 alert_state 去重後貼 ClickUp，error 級 @（PR #79） |
| 每日 08:20 | `com.geothings.geobingan.drainstuck` | 放行我方近 7 天卡住的 pending/failed（先探解析引擎健康、走 retry_parse 預留、上限 20；PR #91） |
| 每日 10:00 | `com.geothings.geobingan.weeklysync` | 完整流程（步驟 1-4）+ 週一加步驟 5 產 PDF |
| 週五 17:00 | `com.geothings.geobingan.fridayreport` | 總結週報 PDF → ClickUp |

安裝：`./setup/setup_launchd.sh` · 卸載：`./setup/uninstall_launchd.sh` · plist 位於 `launchd/`

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
for j in healthcheck drainstuck weeklysync fridayreport; do
  echo "=== $j ==="
  launchctl print "gui/$(id -u)/com.geothings.geobingan.$j" 2>/dev/null \
    | grep -E "runs|last exit|state"
done
```

**自動兜底（PR #53）**：`health_check.py` 加 `check_launchd_jobs()`，每日 08:00 healthcheck 跑時掃所有 job（清單在 `check_launchd_jobs()`，新增 plist 必同步；該清單是 dict，值為**該 job 的正常結束碼**——`drainstuck` 的 `4`＝探測到解析引擎異常、今天不放行，是設計結果不是失敗，不列入就會每天誤報一次）、發現 `last exit != 0` 寫進 ClickUp 通知。6/02 首次真實救援——把原本要等 4 週才被發現的 weeklysync 鎖死提早到 1 天浮現。

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

## 模組依賴關係（#72 restructure 後）

```
根目錄（launchd 進入點，路徑不變）
├── run_weekly_sync.sh / run_friday_report.sh / health_check.py
├── geobingan_sync/                  ← 共用模組 package
│   ├── config.py → .env（REPO_ROOT/.env）
│   ├── city_config.py → data/cities.json
│   ├── drive_utils.py（paginate_files_list 共用翻頁+retry）
│   ├── jwt_auth.py / notify.py / permit_utils.py
│   ├── filename_date_parser.py / sync_status.py / report_template.py
│   ├── analyze_decline.py（被 weekly_snapshot import，故在 package 內）
│   └── steps/                       ← pipeline 步驟（shell 以 python3 -m 呼叫）
│       ├── sync_permits.py / upload_pdfs.py / match_permits.py
│       ├── generate_permit_tracking_report.py / generate_weekly_report.py
│       └── record_sync_result.py / network_ready.py / weekly_snapshot.py
├── tools/cleanup_stale_folders.py   ← 一次性維運工具
├── setup/                           ← setup_launchd / setup_cron / uninstall_launchd
└── data/cities.json
```

呼叫方式：`python3 -m geobingan_sync.steps.<step>`（CWD＝repo root，
`./state`、`./logs` 相對路徑不變；credentials/.env/cities.json 以
`geobingan_sync.REPO_ROOT` 定位、不依賴 CWD）。

### 獨立可測試模組（零外部服務依賴）

| 模組 | 職責 | 測試 |
|------|------|------|
| `permit_utils.py` | normalize_permit + 檔名名稱提取（30+ 預編譯 regex） | 16+13 cases |
| `filename_date_parser.py` | 從 PDF 檔名解析日期（9 種格式，含裸民國年前綴+MMDD） | 27 cases |
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
    ├── PDF: 解析清單來源 → 下載政府 PDF → 智慧分塊解析
    │       resolve_list_pdf_url(list_page_url)：從建管處發布頁抓當前 Download.ashx 連結
    │       候選依序 [動態 → 靜態 pdf_list_url]，各自 retry；raise_for_status + 驗 %PDF 開頭
    │       （PR #78：政府改版會換 relfile 路徑，寫死網址曾同步過期清單 8 個月）
    │       解析後寫 list_fingerprint.json（PR #82）：記錄來源（動態／靜態）、檔名、
    │       建照數與內容 hash；內容變更發資訊性通知，退回靜態或 >60 天未變由
    │       health_check 告警——否則 #78 的 fallback 會靜默退化回原本的 bug
    │       不變量 1：指紋寫入 **fail-closed**——寫不進去就讓同步步驟失敗，不可
    │       吞例外繼續；否則本輪明明退回靜態、health_check 卻仍讀到上輪的
    │       source=動態，silent fallback 原封不動回來
    │       不變量 2：指紋照常更新（health_check 才不會讀到過期的來源資訊），但變更
    │       通知存成 pending_notices，**ClickUp 送達才清除**、否則下輪重試——
    │       若先把指紋存成新 hash 再送，下一輪 changed=False，通知永久遺失
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
Shared Drive
    │
    ▼ list_all_folders()：全量資料夾列表（~1,758 個）
    │  走 drive_utils.paginate_files_list()：nextPageToken 翻頁到底 +
    │  429/5xx 指數退避重試；持久失敗 raise（fail-closed）
    │  ⚠️ 歷史事故：單次呼叫上限 1000、未翻頁靜默截斷 758 個資料夾
    │     → 15+ 建案的 PDF 從未上傳（#65 修復 + catchup 補傳 166 份；
    │        #67/#68 將翻頁收斂為共用 helper，未翻頁的寫法從此絕跡）
    │
    ▼ 批次查詢所有 PDF（~63 次分頁，28,000+ 個）+ folder lookup table 對應資料夾
    │  parent 對不到資料夾表 → 計數 + 顯式告警（不再靜默丟棄）
    │  根目錄 PDF（非建案資料）→ ℹ️ 分開計數，不觸發告警
    │  （批次失敗時回退到逐資料夾查詢〔亦完整翻頁〕；任一資料夾持久失敗
    │    → raise 放棄本次掃描，絕不回傳部分結果〔fail-closed〕）
    │
    ▼ 寫出 state/pdf_inventory.json（已成功對應建案資料夾的 PDF 清單 snapshot，atomic）
    │  （不含根目錄 PDF 與 parent 對不到的 PDF——後兩者僅計數/告警）
    │  → weekly_snapshot 月度趨勢告警 / analyze_decline 的正式資料來源
    │
    ▼ 候選過濾 select_pdfs_to_upload() 純函式（#64）：排除清單 →
    │  history 去重 → run 內同名去重（記檔名供 operator 分辨）→
    │  filename_date_parser 解析檔名日期 →
    │  cutoff 30 天 rolling（正規化當日 00:00；--catchup-days N 可放大補掃）→
    │  max_uploads
    │
    ▼ 解析引擎健康探測（parser_health.probe）：近 24h 我方報告有 billing 失敗，或
    │  pending ≥6h 且期間零 completed → exit 4 今日不上傳（run_weekly_sync 記失敗並告警）
    │  只撞應用層閘門（quota）不擋——那是額度用完的正常結果，午夜重置
    │
    ▼ 解析預算守門（PR #80，詳見「告警送達與預算守門」）：
    │  單次門檻（對原始請求量，超過 BUDGET_CONFIRM_USD 需 --yes）
    │  → 鎖內原子預留（日上限：超過則裁切為今日剩餘可容納份數、耗盡擋下）
    │
    ▼ 逐一下載 → [POST 前一刻：日期仍等於預留日期？否則停批] → 上傳（2 秒間隔）
    │
    ▼ 成功立即寫入 state（flock + merge）；預算帳本：4xx 明確拒絕即時退 1 份，
    │  5xx／逾時／未知保守計入，finally 只退未嘗試份數（綁定預留日期）
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
| `uploaded_to_geobingan_7days.json` | 已上傳 PDF 記錄（legacy 掃描快取於 pdf_inventory 建立後自動移除） | 每次成功上傳 | 否 |
| `pdf_inventory.json` | 建案 PDF inventory（已成功對應建案資料夾的 PDF；不含根目錄/unmatched。月度趨勢/decline 分析資料源；未落地前 consumer fallback 讀 legacy cache） | 每次掃描完成 | 否 |
| `sync_status.json` | 執行狀態與歷史 | 每次執行 | 否 |
| `weekly_snapshots/{date}.json` | sync 後狀態快照（供 compute_diff 算趨勢） | 每次 sync | 否（local-only，見下） |
| `alert_state_healthcheck.json` / `alert_state_sync.json` | 告警去重狀態（各 producer 一個 namespace；key → level/first_seen/last_sent） | 通知真的送達時 | 否 |
| `upload_budget.json` + `.lock` | **今日**解析預算帳本（day/uploaded/**retried**/units/est_usd；flock；**跨日歸零**，**台北午夜**換日；舊月格式檔自動視為非今日而歸零） | 上傳或重推預留／退還時 | 否（重推走 `steps.retry_parse`，它自己預留） |
| `list_fingerprint.json` | 政府清單指紋（source／label／permit_count／sha256／last_changed） | 每次 sync 解析清單後 | 否 |
| `folder_deaths.json` | 來源資料夾由活轉死的紀錄（append-only，附 detected 日期與 pdf_count）。**fail-closed**：先原子寫入此檔成功才提交 registry——順序相反時，中斷會讓 registry 已存 404、下輪 prior 非 alive，該次死亡永遠偵測不到；讀到損毀 JSON 一律 raise，不靜默重置以免丟失歷史。事件以 `(permit, source_url)` 去重（**不含日期**）——death 已寫、registry 提交失敗時，下一輪（排程每日跑，通常是隔天）prior 仍是 alive、會再次偵測到同一次死亡；鍵若含偵測日，去重只在同一天有效。同一建案換新 folder URL 後再失效會自然形成新事件 | 偵測到新失效時 | 否 |

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

## 告警送達與預算守門（PR #79 / #80）

### 為什麼需要（2026-09 復盤）

- 健康檢查與 ClickUp 通道**早就存在、也一直在發**：Token 到期 9/11–9/14 連喊四天、「上傳暫停 N 天」每天一則連發 25 天——全進了沒人訂閱、沒 @ 人的 task。盲點不是「沒偵測」，是**告警沒到達人**。
- 後端 PDF 解析成本受 **OpenAI 專案花費上限**硬性節制（上限不在任何 repo 裡）。2026-09-14 一次上傳 178 份即打爆當日額度，後續 116 份卡 pending 一整天沒人知（PROD-348）。夜間上傳原本 `MAX_UPLOADS=0` 無上限。

### 告警送達（`notify.py` + `alert_state.py`）

```
health_check / record_sync_result
    │  current = {key: (level, message)}   ← 只含非 ok
    ▼
AlertState(namespace=producer).process(current, send)
    │  plan_alerts(prev, current, now)：
    │    新出現 → new；warning→error → escalated；last_sent ≥7 天 → reminder；消失 → resolved
    ▼
send(title, body, needs_mention)   ← needs_mention = 任一 error 級 new/escalated/reminder
    │  needs_attention（error 的新增/升級/提醒/**恢復**）→ Email（實測唯一會推播到人）
    │    ＋ ClickUp 留言（紀錄）；只有 warning → 只留 ClickUp
    │  單一旗標而非 mention/email 兩個：兩者在所有情境恆等，且 @ 對本人是空操作
    │  ClickUp comment array + type:tag block（純文字 @name 不會推播）
    ▼
只有「真正會到人的通道」回報成功才 save(new_state)   ← plan → send → commit
    │  需打擾（error 新增/升級/提醒/**恢復**）→ 看 Email；只有 warning → 看 ClickUp
    │  （未設定 Email 時退回看 ClickUp，避免整條通道卡死不發）
（失敗／例外：保留舊狀態、下一輪重發同事件）
```

不變量（review 定案）：

0. **送達判準要對準真正會到人的通道**（2026-09-16 實測教訓）：原本以 ClickUp @ 為
   送達依據，但機器人用 Zhe 本人的 token 發文——ClickUp 會把自我 @ 的 tag block
   吃成空的 `{"type": "tag"}`、也不通知自己發的留言，等於**結構上不可能送達**
   （Token 到期連喊四天無人察覺即此因）。改為 error 級以 **Email 成功**為判準，
   ClickUp 降為紀錄。教訓：測試只驗「payload 組得對」不夠，必須驗「對方真的收得到」。
   **error 的「已恢復」通知同樣要走 Email**——否則承諾的恢復通知一樣到不了人
   （review P2）；warning 與 warning 恢復維持純紀錄、不打擾。
1. **producer 各自 namespace 狀態檔**——共用時任一方都會把對方的項目判成 resolved、誤發恢復通知，隔天又當新告警重發。
2. **送達才落狀態**——否則 ClickUp 失敗會被抑制 7 天。
3. **乾跑唯讀**（不帶 `--notify`）——手動檢查不可悄悄壓掉之後的告警。

### 預算守門（`budget.py`）

```
upload_pdfs.main()
    │
    ▼ gate_and_reserve(mb, n=len(pdfs), 日上限, 單價, 門檻, --yes, kind='uploaded'|'retried')
    │    1) budget_gate(n)：對「原始請求量」估算，> BUDGET_CONFIRM_USD 且無 --yes → exit 3（尚未預留）
    │       （--yes 到此為止；它**不會**放寬下面的日上限）
    │    2) mb.reserve(n)：flock 內 讀今日餘額 → daily_gate → 立刻計入可放行份數
    │         投影 = 今日已用（上傳＋重推）+ 本次；超過日上限 → 裁切為今日剩餘可容納
    │         份數（0 則擋下、台北午夜重置）；只有 --override-daily-budget REASON 能全放
    ▼ ledger = ReservationLedger(mb, reserved, day=預留日期)
    │
    ▼ 每份：download → before_upload=ledger.begin_item()（POST 前一刻）
    │         當前日期 ≠ 預留日期 → 回 day_rolled_over：不 POST、不寫 error/歷史、停止整批
    │         否則 attempted += 1（視為已消耗）→ upload_to_geobingan
    │       settle(result)：error == 'rejected'（4xx）才 release(1, day)；其餘保留
    ▼ finally ledger.close()：release(reserved − attempted, day)   ← 只退從未嘗試的
```

回應分類（`upload_to_geobingan`）：

| 回應 | 回傳 | 預算處理 |
|------|------|----------|
| 2xx；502/504；逾時/連線錯誤重試耗盡 | dict | 已消耗（可能已送達） |
| 4xx；401 換發失敗且未再送出 | `False` | 確定零成本 → 即時退款 |
| 所有 5xx（含 503 重試耗盡、401 重試後 5xx）；未知例外 | `None` | 結果不明 → 保守計入 |

不變量（八輪 review 定案）：

1. **先預留、後退還**——事後累加會在中斷時漏記；預留即計入，最壞只會多算。
2. **只退確定零成本**——5xx 可能發生在後端已建報告／已進佇列之後。
3. **跨程序鎖 + PID 暫存檔**——排程與人工重疊不可吃到同一筆餘額。
4. **單次門檻對原始請求量**——預留結果必 ≤ 原始量，退還或跨日都無法讓放行超過已確認的量（消除 TOCTOU）。
5. **退還綁定預留日期**——帳本已切新的一天則 no-op，不重建昨日、不動今日。
6. **日期守門放在 POST 前一刻**——下載可耗時數分鐘；跨日停批，剩餘留待新的一天重新預留。
7. **模型要對得上後端的真實節流**（2026-09-16 確認）——後端是**每日 US$20**，不是月上限。用月模型會在還有日額度時無謂擋下上傳。
8. **日界要對到後端閘門的日界，不是猜 provider**——後端閘門是應用層的 `DATA_FOUNDRY_DAILY_BUDGET_USD`，鍵 `data_foundry:spend:{timezone.localdate()}`、`TIME_ZONE=Asia/Taipei`，即**台北日曆日、午夜重置**（2026-09-19 slayer 確認、origin/main 讀碼核對）。#85 曾誤設 UTC，帳本比後端晚 8 小時歸零：方向保守但不對齊。教訓：日界是後端的事實，去讀後端的碼，不要從「OpenAI 應該用 UTC」推。
9. **消耗額度的每條路徑都必須先預留**——手動 `retry-parse` 與上傳共用同一份日額度。事後記帳（`--reconcile-retry`）擋不住競態：在「已送出、尚未記帳」的空窗裡，夜間上傳讀到用量偏低而照常預留，兩者合計即超上限。因此重推走 `steps/retry_parse.py`，用同一個 `DailyBudget.reserve_retry()`（同一把鎖、同一份帳本）先佔額度再送出，並沿用同一套保守結算。
12. **「查不到」不可講成「沒有」**——`retry_parse` 的查詢階段 fail-closed：HTTP 非 200、網路例外、非 JSON、缺 `parse_status` 都進失敗清單而非被跳過。有任何查詢失敗就不宣告「沒有需要重推的報告」，並回 exit 4。原本一律 `except: continue`，整批查詢掛掉時會印出成功訊息並 exit 0，操作者以為積壓清空了。
10. **日界要擋在型別上**——`day_key()` 直接拒收 naive datetime，任何 aware 時間先換算成台北再取日期。時鐘統一走 `budget.budget_now()`；不靠呼叫點自律。
11. **兩道閘不可共用一個旗標**——`--yes` 只確認「這一批很大」，日上限一律強制裁切。若 `--yes` 同時放寬日上限，任何**合法**的大批次都會順帶突破後端硬限：55 份重推估 US$16.5、必須帶 `--yes` 才過單次門檻，今日已用 US$10 時本應只放 32 份，卻會全放 55 份、投影 US$26.5。要真的超支必須另外明講 `--override-daily-budget REASON`，理由會寫進日誌，排程不帶此旗標。
12. **沒有證據 ≠ 健康**——探測只看近 24h 視窗，空視窗原本回「健康」。2026-09-22 實例：前一天 11:00 的 billing 失敗滑出視窗後，探測印「解析引擎正常（completed 0、pending 0）」並放行 15 份，帳戶其實還沒加值。改法＝`state/parser_health.json` 持久化 held；觀測到壞就記、**看見完成才解除**、看不到任何東西就沿用上次狀態。死結出口＝`drain_stuck` 每天送 1 份 canary 探路（成本上限 1 份），或人工 `--skip-parser-health`。canary 的額度**保守結算**（同 `ReservationLedger`）：202 受理與逾時／5xx 的「結果不明」都算用掉，只有全數 4xx 明確拒絕或根本沒送出才允許當日再探——否則同日重跑會重複解析。canary 的 report id 存進 `canary_ids`，探測按 id 補抓（它是幾天前建立的，24h 視窗抓不到）。探測本身失敗時不寫狀態（沒有新資訊，不可清掉 held）。
13. **送進壞掉的佇列比不送更糟**——上傳免費，但撞頂／billing 失敗的解析停在 pending/failed **不會自動恢復**（後端 skip_reason=quota 不重試、午夜重置不放行、retry-parse 端點不查預算）。所以上傳前先探解析引擎健康（`parser_health`），異常就不送；探測本身失敗＝未知，同樣不送（fail-closed）。後端的 `parse_failure_kind` 不可信（billing 被標 invalid_json），只看 parse_error 原文分類。
14. **卡住要能自癒**——`steps/drain_stuck.py` 於 launchd 08:20（台北午夜重置後）只挑**我方**近 7 天上傳的 pending/failed，排除確定性失敗（輸出超上限），先探健康再走 `retry_parse` 的預留。「我方」以檔名比對且**副檔名無關**（Drive 有些檔名沒有 .pdf）。

營運：`DAILY_BUDGET_USD` 須與後端實際日上限對齊（目前 US$20）；`state/upload_budget.json` 為本機狀態，換機用 `python3 -m geobingan_sync.budget --set N` 初始化；`.pause_upload` 可在後端解析停擺或預算未確認時暫停步驟 2。

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

**步驟 4 的分支守門**：這一步 `git push origin main`，但 `git commit` 會落在工作樹
**當下所在的分支**。開發期間工作樹常停在功能分支上，兩者就對不起來——2026-09-17
當天的報告 commit 落到 `feat/daily-budget`，`push origin main` 推了個沒動的 ref
（無聲成功），main 一整天沒收到報告，PR 還被灌進 4070 行狀態檔變動。現在非 `main`
時**一律不 commit**：報告與狀態檔留在工作目錄，回到 main 的下一次執行會一併帶走
（狀態是累積式的，晚一天提交不會遺失資料）。

而且這種情況**記為失敗**（`handle_error`），走既有告警通道。只印 warning 的話
`HAS_ERROR` 仍是 0、cleanup 寫入 success、exit code 是 0、告警不會響，而排程下一次
仍在同一個功能分支上執行——會變成每天跳過發布、線上報告無限期停更卻沒人知道，
等於把「commit 到錯的分支」換成「無聲停止發布」，一樣是無聲失效。

訊息裡的變數一律寫成 `${CURRENT_BRANCH}`。launchd 的環境常沒有 UTF-8 locale，
此時 bash 會把緊接其後的全形字位元組當成變數名的一部分，分支名整個消失、告警變
亂碼，正好在最需要看懂訊息的時候看不懂。

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

所有設定值（`SHARED_DRIVE_ID`、`MAX_UPLOADS`、`DELAY_BETWEEN_UPLOADS`、`CLICKUP_TOKEN`、預算相關的 `COST_PER_REPORT_USD`／`DAILY_BUDGET_USD`／`BUDGET_CONFIRM_USD`；`DAYS_AGO` 為歷史遺留、未參與日期窗計算——cutoff 固定 30 天或由 `--catchup-days` 覆蓋）統一由 `config.py` 從 `.env` 載入，各腳本 import 使用，不再有本地硬編碼覆蓋。多城市配置由 `city_config.py` 從 `cities.json` 載入，空白欄位自動回退到 `.env` 預設值。

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
| `test_resolve_list_pdf_url.py`／`test_download_pdf_list_fallback.py` | 清單動態解析與候選 fallback（PR #78） | 7 | monkeypatch |
| `test_alert_state.py`／`test_notify_mention.py`／`test_health_check_dedup.py` | 告警去重/升級、送達才落狀態、namespace 分離、mention payload（PR #79） | 23 | tmp_path |
| `test_budget.py`／`test_check_parse_backlog.py`／`test_process_single_pdf_classification.py`／`test_upload_response_classification.py` | 預算守門（預留/退還/跨日/POST 前守門/8 子程序併發/重推與上傳共用日額度）、解析積壓與預算檢查、回應分類（PR #80） | 40+ | tmp_path, subprocess |
| `test_list_fingerprint.py`／`test_folder_deaths.py` | 清單指紋（變更／靜態退回／停更／寫入 fail-closed／通知 pending 重試）、來源資料夾由活轉死（fail-closed 排序／事件去重不含日期／損毀不重置）（PR #82） | 28 | tmp_path |
| `test_email_alert.py` | Email 通道（組信 MIME／未設定／SMTP 成功失敗／error 以 Email 判送達／warning 不寄信／未設定退回 ClickUp／**error 恢復走 Email 且失敗重試**） | 9 | monkeypatch |
| **合計** | | **306** | |

設計原則：
- **測試不得寫進正式落地路徑**——`download_pdf_list(dest=…)` 供測試注入；2026-09-16 曾因測試把 `/tmp/permit_list.pdf` 覆寫成 37 bytes 假檔，導致以該檔做的人工判讀誤判某建案已從政府清單下架
- 所有測試 import 零依賴模組（`permit_utils`、`drive_utils`），不觸發 credentials 或 Google API（lazy init）
- 可在 CI（無 credentials.json）或乾淨環境執行
- Smoke tests 覆蓋報告生成端到端路徑（含 XSS escaping、file round-trip）
- `normalize_permit` 和 `extract_name_from_filename` 統一在 `permit_utils.py`，消除 test import side effects
