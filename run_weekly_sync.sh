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

# 遇到錯誤不立即退出，改為手動處理
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

# 啟動虛擬環境
source "$SCRIPT_DIR/venv/bin/activate"

# 記錄開始執行
python3 -c "
from sync_status import SyncStatus
status = SyncStatus()
status.start_run()
" 2>&1 | tee -a "$LOG_FILE"

# 步驟 1: 同步 PDF 從台北市政府到 Google Drive
echo "" | tee -a "$LOG_FILE"
echo "📥 步驟 1/4: 同步 PDF 從台北市政府網站..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
if ! python3 "$SCRIPT_DIR/sync_permits.py" 2>&1 | tee -a "$LOG_FILE"; then
    handle_error "步驟1" "同步 PDF 失敗"
fi

# 從日誌解析同步數量
SYNCED_COUNT=$(grep -o "新增 [0-9]* 個 PDF" "$LOG_FILE" | tail -1 | grep -o "[0-9]*" || echo "0")

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
echo "" | tee -a "$LOG_FILE"
echo "📤 步驟 2/4: 上傳最近 7 天的 PDF 到 Backend..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
if ! python3 "$SCRIPT_DIR/upload_pdfs.py" 2>&1 | tee -a "$LOG_FILE"; then
    handle_error "步驟2" "上傳 PDF 失敗"
fi

# 從日誌解析上傳數量
UPLOADED_COUNT=$(grep -c "報告上傳成功" "$LOG_FILE" || echo "0")
FAILED_COUNT=$(grep -c "上傳失敗" "$LOG_FILE" || echo "0")

# 步驟 3: 生成建照監測追蹤報告
echo "" | tee -a "$LOG_FILE"
echo "📊 步驟 3/4: 生成建照監測追蹤報告..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
if ! python3 "$SCRIPT_DIR/generate_permit_tracking_report.py" 2>&1 | tee -a "$LOG_FILE"; then
    handle_error "步驟3" "生成報告失敗"
fi

# 步驟 4: 更新線上報告到 GitHub
echo "" | tee -a "$LOG_FILE"
echo "🌐 步驟 4/4: 更新線上報告到 GitHub..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"

# 複製報告到 docs 目錄
cp "$SCRIPT_DIR/state/permit_tracking_report.html" "$SCRIPT_DIR/docs/index.html"
echo "✅ 已複製報告到 docs/index.html" | tee -a "$LOG_FILE"

# 提交並推送到 GitHub
cd "$SCRIPT_DIR"
if git diff --quiet docs/index.html 2>/dev/null; then
    echo "ℹ️  報告無變更，跳過推送" | tee -a "$LOG_FILE"
else
    git add docs/index.html state/permit_tracking_report.html state/permit_tracking.csv
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

# 計算執行時間
END_TIME=$(date +%s)
DURATION_SECONDS=$((END_TIME - START_TIME))
DURATION_MINUTES=$(echo "scale=1; $DURATION_SECONDS / 60" | bc)

# 記錄執行結果並發送通知
echo "" | tee -a "$LOG_FILE"
echo "📝 記錄執行結果..." | tee -a "$LOG_FILE"

if [ $HAS_ERROR -eq 0 ]; then
    STATUS="success"
else
    STATUS="failure"
fi

python3 << EOF 2>&1 | tee -a "$LOG_FILE"
from sync_status import SyncStatus
from notify import send_success, send_failure

status = SyncStatus()
result = status.end_run(
    status='$STATUS',
    synced_pdfs=$SYNCED_COUNT,
    uploaded_pdfs=$UPLOADED_COUNT,
    failed_uploads=$FAILED_COUNT,
    error_message='$ERROR_MESSAGE' if '$STATUS' == 'failure' else None
)

# 發送通知
if '$STATUS' == 'success':
    send_success(
        synced=$SYNCED_COUNT,
        uploaded=$UPLOADED_COUNT,
        failed=$FAILED_COUNT,
        duration_minutes=$DURATION_MINUTES
    )
else:
    send_failure('執行失敗', '$ERROR_MESSAGE')
EOF

# 完成
echo "" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"
if [ $HAS_ERROR -eq 0 ]; then
    echo "✅ 週期同步執行完成 - $(date)" | tee -a "$LOG_FILE"
else
    echo "⚠️ 週期同步執行完成（有錯誤） - $(date)" | tee -a "$LOG_FILE"
fi
echo "執行時間: ${DURATION_MINUTES} 分鐘" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# 清理超過 30 天的舊日誌
find "$LOG_DIR" -name "weekly_sync_*.log" -mtime +30 -delete

exit $HAS_ERROR
