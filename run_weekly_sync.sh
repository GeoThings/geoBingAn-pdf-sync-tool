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

# 日誌檔案（使用日期時間命名）
LOG_FILE="$LOG_DIR/weekly_sync_$(date +%Y%m%d_%H%M%S).log"

# 狀態變數
SYNCED_COUNT=0
UPLOADED_COUNT=0
FAILED_COUNT=0
HAS_ERROR=0
ERROR_MESSAGE=""
START_TIME=$(date +%s)

# 清理函數（在腳本結束時執行）
cleanup() {
    local exit_code=$?

    # 計算執行時間
    END_TIME=$(date +%s)
    DURATION_SECONDS=$((END_TIME - START_TIME))

    # 如果有未捕獲的錯誤
    if [ $exit_code -ne 0 ] && [ $HAS_ERROR -eq 0 ]; then
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
    export SYNC_SYNCED_COUNT="$SYNCED_COUNT"
    export SYNC_UPLOADED_COUNT="$UPLOADED_COUNT"
    export SYNC_FAILED_COUNT="$FAILED_COUNT"
    export SYNC_DURATION_SECONDS="$DURATION_SECONDS"
    export SYNC_ERROR_MESSAGE="$ERROR_MESSAGE"

    echo "" | tee -a "$LOG_FILE"
    echo "📝 記錄執行結果..." | tee -a "$LOG_FILE"

    # 使用獨立的 Python 腳本處理通知和狀態記錄
    python3 "$SCRIPT_DIR/record_sync_result.py" 2>&1 | tee -a "$LOG_FILE" || true

    # 完成訊息
    echo "" | tee -a "$LOG_FILE"
    echo "========================================" | tee -a "$LOG_FILE"
    DURATION_MINUTES=$(echo "scale=1; $DURATION_SECONDS / 60" | bc)
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
echo "========================================" | tee -a "$LOG_FILE"

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
from sync_status import SyncStatus
status = SyncStatus()
status.start_run()
" 2>&1 | tee -a "$LOG_FILE"

# 檢查 Refresh Token 有效期
echo "" | tee -a "$LOG_FILE"
echo "🔑 檢查 Token 有效期..." | tee -a "$LOG_FILE"
# 暫時關閉 errexit 以取得 Python exit code（非零不代表腳本錯誤）
set +e
TOKEN_CHECK=$(python3 -c "
from jwt_auth import decode_jwt_payload
from config import REFRESH_TOKEN
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
from notify import send_notification
send_notification('❌ geoBingAn Token 已過期', 'Refresh Token 已過期，請登入 riskmap.today 取得新 Token 並更新 .env')
" 2>&1 | tee -a "$LOG_FILE" || true
    handle_error "Token 檢查" "Refresh Token 已過期"
    exit 1
elif [ $TOKEN_EXIT -eq 1 ]; then
    DAYS=$(echo "$TOKEN_CHECK" | grep -o '[0-9.]*')
    echo "⚠️  Refresh Token 將在 ${DAYS} 天後過期，請儘快更新" | tee -a "$LOG_FILE"
    python3 -c "
from notify import send_notification
send_notification('⚠️ geoBingAn Token 即將過期', 'Refresh Token 將在 ${DAYS} 天後過期，請登入 riskmap.today 更新 .env 中的 Token')
" 2>&1 | tee -a "$LOG_FILE" || true
    echo "   繼續執行同步流程..." | tee -a "$LOG_FILE"
else
    DAYS=$(echo "$TOKEN_CHECK" | grep -o '[0-9.]*')
    echo "✅ Refresh Token 有效期剩餘 ${DAYS} 天" | tee -a "$LOG_FILE"
fi

# 步驟 1: 同步 PDF 從台北市政府到 Google Drive
STEP1_FAILED=0
echo "" | tee -a "$LOG_FILE"
echo "📥 步驟 1/4: 同步 PDF 從台北市政府網站..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
if ! python3 "$SCRIPT_DIR/sync_permits.py" 2>&1 | tee -a "$LOG_FILE"; then
    handle_error "步驟1" "同步 PDF 失敗"
    STEP1_FAILED=1
fi

# 從日誌解析同步數量
SYNCED_COUNT=$(grep -o "新增 [0-9]* 個 PDF" "$LOG_FILE" 2>/dev/null | tail -1 | grep -o "[0-9]*" || echo "0")

# 如果步驟 1 失敗，跳過後續依賴步驟
if [ $STEP1_FAILED -ne 0 ]; then
    echo "⚠️  步驟 1 失敗，跳過步驟 2-3（依賴同步資料）" | tee -a "$LOG_FILE"
else

# 清除 PDF 快取（確保偵測到新同步的檔案）
echo "" | tee -a "$LOG_FILE"
echo "🗑️  清除 PDF 快取..." | tee -a "$LOG_FILE"
python3 -c "
import json
state_file = './state/uploaded_to_geobingan_7days.json'
try:
    with open(state_file, 'r') as f:
        state = json.load(f)
    if 'cache' in state:
        state['cache'] = {'folders': [], 'pdfs': [], 'last_scan': None}
        with open(state_file, 'w') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        print('✅ 快取已清除')
except Exception as e:
    print(f'⚠️ 清除快取時發生錯誤: {e}')
" 2>&1 | tee -a "$LOG_FILE"

# 步驟 2: 上傳最近 7 天的 PDF 到 geoBingAn Backend
STEP2_FAILED=0
echo "" | tee -a "$LOG_FILE"
echo "📤 步驟 2/4: 上傳最近 7 天的 PDF 到 Backend..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
if ! python3 "$SCRIPT_DIR/upload_pdfs.py" 2>&1 | tee -a "$LOG_FILE"; then
    handle_error "步驟2" "上傳 PDF 失敗"
    STEP2_FAILED=1
fi

# 從日誌解析上傳數量（確保只取單一數字）
UPLOADED_COUNT=$(grep -c "報告上傳成功" "$LOG_FILE" 2>/dev/null | head -1 | tr -d '\n' || echo "0")
FAILED_COUNT=$(grep -c "上傳失敗" "$LOG_FILE" 2>/dev/null | head -1 | tr -d '\n' || echo "0")
# 確保是有效數字
[[ "$UPLOADED_COUNT" =~ ^[0-9]+$ ]] || UPLOADED_COUNT=0
[[ "$FAILED_COUNT" =~ ^[0-9]+$ ]] || FAILED_COUNT=0

# 步驟 2.5: 建案名稱交叉比對
echo "" | tee -a "$LOG_FILE"
echo "🔍 步驟 2.5: 建案名稱交叉比對..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
if ! python3 "$SCRIPT_DIR/match_permits.py" 2>&1 | tee -a "$LOG_FILE"; then
    echo "⚠️  名稱比對失敗，使用現有 registry 繼續" | tee -a "$LOG_FILE"
fi

# 儲存快照 + 偵測新建案
echo "" | tee -a "$LOG_FILE"
echo "📸 儲存快照 + 偵測新建案..." | tee -a "$LOG_FILE"
python3 "$SCRIPT_DIR/weekly_snapshot.py" --notify 2>&1 | tee -a "$LOG_FILE" || true

# 步驟 3: 生成建照監測追蹤報告
# 如果步驟 2 失敗，跳過報告生成（會使用不完整的資料）
if [ $STEP2_FAILED -ne 0 ]; then
    echo "" | tee -a "$LOG_FILE"
    echo "⚠️  步驟 2 失敗，跳過步驟 3（報告會使用不完整的資料）" | tee -a "$LOG_FILE"
else
echo "" | tee -a "$LOG_FILE"
echo "📊 步驟 3/6: 生成建照監測追蹤報告..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
if ! python3 "$SCRIPT_DIR/generate_permit_tracking_report.py" 2>&1 | tee -a "$LOG_FILE"; then
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
git add docs/index.html state/permit_tracking_report.html state/permit_tracking.csv state/upload_history_all.json state/permit_registry.json state/weekly_snapshots/ 2>/dev/null || true
if git diff --cached --quiet 2>/dev/null; then
    echo "ℹ️  無任何變更，跳過推送" | tee -a "$LOG_FILE"
else
    if git commit -m "Weekly sync report update ($(date +%Y-%m-%d))" 2>&1 | tee -a "$LOG_FILE"; then
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

# 步驟 5: 產生週報 PDF 並上傳到 ClickUp
echo "" | tee -a "$LOG_FILE"
echo "📄 步驟 5/5: 產生同步週報 PDF..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
if python3 "$SCRIPT_DIR/generate_weekly_report.py" --type sync --upload 2>&1 | tee -a "$LOG_FILE"; then
    echo "✅ 週報已上傳到 ClickUp" | tee -a "$LOG_FILE"
else
    echo "⚠️  週報產生或上傳失敗（不影響同步結果）" | tee -a "$LOG_FILE"
fi

# cleanup 會在 EXIT trap 中執行
