#!/bin/bash
#
# geoBingAn PDF 週期同步執行腳本
# 用途：每週執行 PDF 同步和上傳流程
#
# 執行順序：
# 1. sync_permits.py - 從台北市政府網站同步最新建案 PDF 到 Google Drive
# 2. upload_pdfs.py - 上傳最近 7 天更新的 PDF 到 geoBingAn Backend
# 3. generate_permit_tracking_report.py - 生成建照監測追蹤報告
# 4. 更新線上報告 - 推送到 GitHub 自動更新線上版本
#
# 功能：
# - 自動狀態追蹤 (state/sync_status.json)
# - 失敗通知 (LINE Notify / macOS)
# - 完成摘要通知
#

# 關鍵步驟失敗即中止
set -e
set -o pipefail

# 切換到腳本所在目錄
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# 日誌目錄
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

# Diagnostic trigger marker — 永遠寫入、不依賴後續 setup 成功
# 4 月某次 launchd 跑 weeklysync 失敗 (EX_CONFIG 78) → 進 backoff 鎖死 4 週、5/25 才發現
# PPID 區分觸發來源：launchd spawn 的 PPID 是 user-level launchd（通常低 PID，非 1）、手動跑是 user shell 高 PID
# 鏡像 PR #49 對 run_friday_report.sh 的處理（兩個 job plist 結構相同）
echo "[$(date '+%F %T')] === run_weekly_sync.sh triggered (PID=$$, PPID=$PPID) ===" >> "$LOG_DIR/weeklysync_trigger.log"

# 日誌檔案（使用日期時間命名）
LOG_FILE="$LOG_DIR/weekly_sync_$(date +%Y%m%d_%H%M%S).log"

# ── 整體執行時間上限（看門狗）────────────────────────────────────────────
# 2026-09-27 實際踩到：Drive 掃描遇到 51 次 Connection reset，每次請求各自重試，
# 但**整體沒有上限**，一路跑了 734.6 分鐘（12.2 小時）才放棄。後果：
#   1. 壓到隔天的排程（launchd 10:00 又起一份，兩份並跑搶同一份狀態檔）
#   2. 失敗通知隔天才到，等於失去告警意義
# 本機沒有 GNU timeout（coreutils 未安裝），所以自己看門。邏輯在 lib/watchdog.sh
# （抽出來才測得到；兩個 review P1 的理由也寫在那裡）。
MAX_RUNTIME_SECONDS="${MAX_RUNTIME_SECONDS:-3600}"     # 預設 60 分（正常 15–17 分）
TIMEOUT_GRACE_SECONDS="${TIMEOUT_GRACE_SECONDS:-60}"   # SIGTERM 到 SIGKILL 的寬限
TIMEOUT_MARKER="$LOG_DIR/.weeklysync_timeout.$$"
WAS_TIMEOUT=0
# shellcheck source=lib/watchdog.sh
source "$SCRIPT_DIR/lib/watchdog.sh"

# 狀態變數
# 計數不再從日誌 grep（2026-09-30：三個樣式都靜默失準，見 geobingan_sync/step_results.py）。
# 各步驟把數字寫進 state/step_result_*.json，record_sync_result 讀資料。
# 這裡只傳「本輪何時開始」讓它判斷結果檔是本輪的，以及上傳是否被跳過。
UPLOAD_SKIPPED=""
HAS_ERROR=0
ERROR_MESSAGE=""
START_TIME=$(date +%s)
# ISO 格式的開始時間：record_sync_result 用它判斷結果檔是本輪寫的還是上一輪殘留。
# 沿用舊檔會把昨天的數字報成今天的，比報「未取得」更糟。
RUN_STARTED_ISO=$(date -Iseconds 2>/dev/null || date +%Y-%m-%dT%H:%M:%S)

# 今天是週幾（ISO: 1=Mon..7=Sun）— commit message 標籤 + 步驟 5 是否產 PDF 都用這個
# WEEKDAY 取值若失敗（極罕）會是空字串、後續比較 "1" 為 false、保守視為非週一（skip PDF + Daily label）
WEEKDAY=$(date +%u)
if [ "$WEEKDAY" = "1" ]; then
    COMMIT_LABEL="Weekly sync"
else
    COMMIT_LABEL="Daily sync"
fi

# 清理函數（在腳本結束時執行）
cleanup() {
    local exit_code=$?

    # 計算執行時間
    END_TIME=$(date +%s)
    DURATION_SECONDS=$((END_TIME - START_TIME))

    # 如果有未捕獲的錯誤
    # 超時要講清楚是超時，不可混進「未預期的錯誤」——操作者會去查錯的方向
    if [ -f "$TIMEOUT_MARKER" ]; then
        rm -f "$TIMEOUT_MARKER"
        WAS_TIMEOUT=1
        HAS_ERROR=1
        ERROR_MESSAGE="執行超過 ${MAX_RUNTIME_SECONDS}s 上限被終止（正常 15–17 分；多為 Drive 連線重置後無上限重試）"
    elif [ $exit_code -ne 0 ] && [ $HAS_ERROR -eq 0 ]; then
        HAS_ERROR=1
        ERROR_MESSAGE="未預期的錯誤 (exit code: $exit_code)"
    fi

    # 決定狀態
    local STATUS="success"
    if [ $HAS_ERROR -ne 0 ]; then
        STATUS="failure"
    fi

    # 使用環境變數傳遞給 Python，避免字串注入問題
    export SYNC_STATUS="$STATUS"
    export SYNC_RUN_STARTED="${RUN_STARTED_ISO}"
    export SYNC_UPLOAD_SKIPPED="${UPLOAD_SKIPPED}"
    export SYNC_DURATION_SECONDS="$DURATION_SECONDS"
    export SYNC_ERROR_MESSAGE="$ERROR_MESSAGE"

    echo "" | tee -a "$LOG_FILE"
    echo "📝 記錄執行結果..." | tee -a "$LOG_FILE"

    # 使用獨立的 Python 腳本處理通知和狀態記錄
    python3 -m geobingan_sync.steps.record_sync_result 2>&1 | tee -a "$LOG_FILE" || true

    # 完成訊息
    echo "" | tee -a "$LOG_FILE"
    echo "========================================" | tee -a "$LOG_FILE"
    DURATION_MINUTES=$(echo "scale=1; $DURATION_SECONDS / 60" | bc)
    # 看門狗在這裡才取消，而且只在**非超時**路徑取消（review P1-2）：
    #  - 取消得太早 → cleanup／通知卡住時沒人殺它，宣稱的 grace 是假的
    #  - 超時路徑不取消 → 讓 wd 的 grace→SIGKILL 真的生效
    #  - 正常路徑放最後 → cleanup 本身也受同一個期限保護
    if [ "${WAS_TIMEOUT}" -eq 0 ]; then
        wd_stop
    fi

    if [ $HAS_ERROR -eq 0 ]; then
        echo "✅ 週期同步執行完成 - $(date)" | tee -a "$LOG_FILE"
    else
        echo "⚠️ 週期同步執行完成（有錯誤） - $(date)" | tee -a "$LOG_FILE"
        echo "錯誤訊息: $ERROR_MESSAGE" | tee -a "$LOG_FILE"
    fi
    echo "執行時間: ${DURATION_MINUTES} 分鐘" | tee -a "$LOG_FILE"
    echo "========================================" | tee -a "$LOG_FILE"
    echo "" | tee -a "$LOG_FILE"

    # 清理超過 30 天的舊日誌
    find "$LOG_DIR" -name "weekly_sync_*.log" -mtime +30 -delete 2>/dev/null || true

    exit $HAS_ERROR
}

# 註冊清理函數
trap cleanup EXIT

# 錯誤處理函數
handle_error() {
    local step="$1"
    local message="$2"
    HAS_ERROR=1
    ERROR_MESSAGE="$step: $message"
    echo "❌ 錯誤: $ERROR_MESSAGE" | tee -a "$LOG_FILE"
}

echo "========================================" | tee -a "$LOG_FILE"
echo "🚀 開始執行週期同步 - $(date)" | tee -a "$LOG_FILE"
echo "   ⏱️  執行時間上限 ${MAX_RUNTIME_SECONDS}s" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"
# SIGTERM 時走正常的 cleanup 並回確定的結束碼。
# 不設的話 bash 會以 128+15=143 結束，巡檢看到 143 會誤導成「backoff 鎖死」。
trap 'exit 1' TERM
wd_start

# 啟動虛擬環境（關鍵步驟，失敗會觸發 set -e 退出）
if [ ! -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    handle_error "初始化" "找不到虛擬環境"
    exit 1
fi
source "$SCRIPT_DIR/venv/bin/activate"

# 預編譯 .pyc（避免 Python 升級後首次執行 import 極慢，跳過 venv）
python3 -m compileall -q -x 'venv|__pycache__|\.git' "$SCRIPT_DIR" 2>/dev/null || true

# 記錄開始執行
python3 -c "
from geobingan_sync.sync_status import SyncStatus
status = SyncStatus()
status.start_run()
" 2>&1 | tee -a "$LOG_FILE"

# 檢查 Refresh Token 有效期
echo "" | tee -a "$LOG_FILE"
echo "🔑 檢查 Token 有效期..." | tee -a "$LOG_FILE"
# 暫時關閉 errexit 以取得 Python exit code（非零不代表腳本錯誤）
set +e
TOKEN_CHECK=$(python3 -c "
from geobingan_sync.jwt_auth import decode_jwt_payload
from geobingan_sync.config import REFRESH_TOKEN
import time, sys
payload = decode_jwt_payload(REFRESH_TOKEN)
exp = payload.get('exp', 0)
days_left = (exp - time.time()) / 86400
if days_left < 0:
    print(f'EXPIRED:{-days_left:.1f}')
    sys.exit(2)
elif days_left < 2:
    print(f'WARNING:{days_left:.1f}')
    sys.exit(1)
else:
    print(f'OK:{days_left:.1f}')
    sys.exit(0)
" 2>&1)
TOKEN_EXIT=$?
set -e

if [ $TOKEN_EXIT -eq 2 ]; then
    DAYS=$(echo "$TOKEN_CHECK" | grep -o '[0-9.]*')
    echo "❌ Refresh Token 已過期 ${DAYS} 天，請登入 riskmap.today 更新" | tee -a "$LOG_FILE"
    python3 -c "
from geobingan_sync.notify import send_notification
send_notification('❌ geoBingAn Token 已過期', 'Refresh Token 已過期，請登入 riskmap.today 取得新 Token 並更新 .env', use_clickup=True, mention=True)
" 2>&1 | tee -a "$LOG_FILE" || true
    handle_error "Token 檢查" "Refresh Token 已過期"
    exit 1
elif [ $TOKEN_EXIT -eq 1 ]; then
    DAYS=$(echo "$TOKEN_CHECK" | grep -o '[0-9.]*')
    echo "⚠️  Refresh Token 將在 ${DAYS} 天後過期，請儘快更新" | tee -a "$LOG_FILE"
    python3 -c "
from geobingan_sync.notify import send_notification
send_notification('⚠️ geoBingAn Token 即將過期', 'Refresh Token 將在 ${DAYS} 天後過期，請登入 riskmap.today 更新 .env 中的 Token', use_clickup=True)
" 2>&1 | tee -a "$LOG_FILE" || true
    echo "   繼續執行同步流程..." | tee -a "$LOG_FILE"
else
    DAYS=$(echo "$TOKEN_CHECK" | grep -o '[0-9.]*')
    echo "✅ Refresh Token 有效期剩餘 ${DAYS} 天" | tee -a "$LOG_FILE"
fi

# 網路就緒檢查：阻塞等 DNS resolver ready 再開跑，根治 launchd post-wake DNS race (#59)
# 即使逾時也只記警告、不 abort（交由步驟 1 既有錯誤處理）
echo "" | tee -a "$LOG_FILE"
set +e
python3 -m geobingan_sync.steps.network_ready 2>&1 | tee -a "$LOG_FILE"
set -e

# 步驟 1: 同步 PDF 從台北市政府到 Google Drive
STEP1_FAILED=0
echo "" | tee -a "$LOG_FILE"
echo "📥 步驟 1/4: 同步 PDF 從台北市政府網站..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
if ! python3 -m geobingan_sync.steps.sync_permits 2>&1 | tee -a "$LOG_FILE"; then
    handle_error "步驟1" "同步 PDF 失敗"
    STEP1_FAILED=1
fi

# 如果步驟 1 失敗，跳過後續依賴步驟
if [ $STEP1_FAILED -ne 0 ]; then
    echo "⚠️  步驟 1 失敗，跳過步驟 2-3（依賴同步資料）" | tee -a "$LOG_FILE"
else

# （原「清除 PDF 快取」區塊已移除：掃描快取機制已拆除，
#   且在 inventory 建立前清空 legacy cache 會毀掉月度告警的 fallback 資料源）

# 步驟 2: 上傳最近 7 天的 PDF 到 geoBingAn Backend
# 可用 .pause_upload 旗標檔暫停上傳（例如後端 AI 停用期間，避免堆積 parse 失敗的報告）。
# 恢復方式：rm .pause_upload。同步其餘步驟（1 / 2.5 / 3 / 4 / 5）照常執行。
STEP2_FAILED=0
if [ -f "$SCRIPT_DIR/.pause_upload" ]; then
    echo "" | tee -a "$LOG_FILE"
    echo "⏸️  步驟 2/4: 上傳已暫停（偵測到 .pause_upload 旗標，恢復：rm .pause_upload）" | tee -a "$LOG_FILE"
    sed 's/^/   /' "$SCRIPT_DIR/.pause_upload" 2>/dev/null | tee -a "$LOG_FILE" || true
    # 跳過是「確定沒上傳」，不是「沒量到」——要讓摘要報 0 而不是未取得
    UPLOAD_SKIPPED="paused"
else
    echo "" | tee -a "$LOG_FILE"
    echo "📤 步驟 2/4: 上傳最近 7 天的 PDF 到 Backend..." | tee -a "$LOG_FILE"
    echo "----------------------------------------" | tee -a "$LOG_FILE"
    if python3 -m geobingan_sync.steps.upload_pdfs 2>&1 | tee -a "$LOG_FILE"; then
        :
    else
        # PIPESTATUS[0] 是 python 的結束碼（pipefail 下 $? 也是，但這裡明確取）。
        # 5＝EXIT_PARSER_HELD，解析引擎探測擋下（帳戶沒餘額／worker 停擺）：要告警
        # 但訊息要講清楚，不能跟「上傳失敗」混在一起，否則操作者會去查上傳而不是後端。
        UPLOAD_RC=${PIPESTATUS[0]}
        if [ "${UPLOAD_RC}" -eq 5 ]; then
            handle_error "步驟2" "解析引擎異常，今日上傳已暫停（見上方探測結果；後端修復後會自動恢復）"
        else
            handle_error "步驟2" "上傳 PDF 失敗（exit ${UPLOAD_RC}）"
        fi
        STEP2_FAILED=1
    fi
fi

# 步驟 2.5: 建案名稱交叉比對
echo "" | tee -a "$LOG_FILE"
echo "🔍 步驟 2.5: 建案名稱交叉比對..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
if ! python3 -m geobingan_sync.steps.match_permits 2>&1 | tee -a "$LOG_FILE"; then
    echo "⚠️  名稱比對失敗，使用現有 registry 繼續" | tee -a "$LOG_FILE"
fi

# 儲存快照 + 偵測新建案
echo "" | tee -a "$LOG_FILE"
echo "📸 儲存快照 + 偵測新建案..." | tee -a "$LOG_FILE"
python3 -m geobingan_sync.steps.weekly_snapshot --notify 2>&1 | tee -a "$LOG_FILE" || true

# 步驟 3: 生成建照監測追蹤報告
# 如果步驟 2 失敗，跳過報告生成（會使用不完整的資料）
if [ $STEP2_FAILED -ne 0 ]; then
    echo "" | tee -a "$LOG_FILE"
    echo "⚠️  步驟 2 失敗，跳過步驟 3（報告會使用不完整的資料）" | tee -a "$LOG_FILE"
else
echo "" | tee -a "$LOG_FILE"
echo "📊 步驟 3/6: 生成建照監測追蹤報告..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
if ! python3 -m geobingan_sync.steps.generate_permit_tracking_report 2>&1 | tee -a "$LOG_FILE"; then
    handle_error "步驟3" "生成報告失敗"
fi
fi  # end STEP2_FAILED check

fi  # end STEP1_FAILED check

# 步驟 4: 更新線上報告到 GitHub
echo "" | tee -a "$LOG_FILE"
echo "🌐 步驟 4/4: 更新線上報告到 GitHub..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"

# 複製報告到 docs 目錄
if [ -f "$SCRIPT_DIR/state/permit_tracking_report.html" ]; then
    if ! cp "$SCRIPT_DIR/state/permit_tracking_report.html" "$SCRIPT_DIR/docs/index.html"; then
        handle_error "步驟4" "複製報告失敗"
    else
        echo "✅ 已複製報告到 docs/index.html" | tee -a "$LOG_FILE"
    fi
else
    echo "⚠️ 找不到報告檔案，跳過複製" | tee -a "$LOG_FILE"
fi

# 提交並推送到 GitHub（檢查報告或上傳歷史是否有變更）
cd "$SCRIPT_DIR"

# 分支守門：本步驟固定 push 到 main，但 commit 會落在工作樹「當下所在的分支」。
# 開發期間工作樹常停在功能分支上（2026-09-17 就把當天的報告 commit 到
# feat/daily-budget，main 一整天沒收到報告、PR 還被灌進 4070 行狀態檔變動）。
# 因此非 main 時一律不 commit：檔案留在工作目錄不動，下一次在 main 上執行會一併帶走。
#
# 而且**必須記成失敗**（review P1）。只印 warning 的話 HAS_ERROR 仍是 0：cleanup
# 會寫入 success、exit code 是 0、既有告警不會響，而 launchd 下一次仍在同一個功能
# 分支上執行。結果會是每天都跳過發布、線上報告無限期停更，卻沒有任何人知道——等於
# 把「commit 到錯的分支」換成「無聲停止發布」，同樣是無聲失效。
# 注意：下面一律用 ${CURRENT_BRANCH} 大括號形式。launchd 的環境常沒有 UTF-8 locale，
# 此時 bash 會把緊接在後的全形字（如「（」）的位元組當成變數名的一部分，分支名會
# 整個消失、告警訊息變亂碼——正好在最需要看懂訊息的時候看不懂。
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "⚠️  工作樹目前在分支 ${CURRENT_BRANCH}（非 main），跳過 commit/push。" | tee -a "$LOG_FILE"
    echo "    報告與狀態檔已更新在工作目錄，回到 main 後的下一次執行會一併提交。" | tee -a "$LOG_FILE"
    echo "    解法：cd $SCRIPT_DIR && git checkout main" | tee -a "$LOG_FILE"
    handle_error "步驟4" "工作樹在 ${CURRENT_BRANCH}（非 main），未發布線上報告；請切回 main"
elif ! git add docs/index.html state/permit_tracking_report.html state/permit_tracking.csv state/upload_history_all.json state/permit_registry.json 2>/dev/null; then
    echo "ℹ️  沒有可提交的報告檔案，跳過推送" | tee -a "$LOG_FILE"
elif git diff --cached --quiet 2>/dev/null; then
    echo "ℹ️  無任何變更，跳過推送" | tee -a "$LOG_FILE"
else
    if git commit -m "$COMMIT_LABEL report update ($(date +%Y-%m-%d))" 2>&1 | tee -a "$LOG_FILE"; then
        if git push origin main 2>&1 | tee -a "$LOG_FILE"; then
            echo "✅ 已推送到 GitHub" | tee -a "$LOG_FILE"
            echo "🔗 線上報告: https://htmlpreview.github.io/?https://github.com/GeoThings/geoBingAn-pdf-sync-tool/blob/main/docs/index.html" | tee -a "$LOG_FILE"
        else
            handle_error "步驟4" "推送到 GitHub 失敗"
        fi
    else
        handle_error "步驟4" "Git commit 失敗"
    fi
fi

# 步驟 5: 產生 sync 週報 PDF 並上傳到 ClickUp（只週一執行）
# 週二-週日：跳過 PDF 步驟、避免 ClickUp 每天都有重複附件
# 週五另有 fridayreport launchd job 產 summary 週報 (--type summary)
echo "" | tee -a "$LOG_FILE"
if [ "$WEEKDAY" = "1" ]; then
    echo "📄 步驟 5/5: 產生 sync 週報 PDF（週一例行）..." | tee -a "$LOG_FILE"
    echo "----------------------------------------" | tee -a "$LOG_FILE"
    if python3 -m geobingan_sync.steps.generate_weekly_report --type sync --upload 2>&1 | tee -a "$LOG_FILE"; then
        echo "✅ 週報已上傳到 ClickUp" | tee -a "$LOG_FILE"
    else
        echo "⚠️  週報產生或上傳失敗（不影響同步結果）" | tee -a "$LOG_FILE"
    fi
else
    echo "ℹ️  步驟 5/5: 跳過 sync 週報 PDF（非週一、避免每日重複附件）" | tee -a "$LOG_FILE"
fi

# cleanup 會在 EXIT trap 中執行
