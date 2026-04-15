#!/bin/bash
#
# 週五總結週報產生腳本
# 每週五 18:00 由 cron 觸發
# 產生整週總結 PDF 並上傳到 ClickUp
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/friday_report_$(date +%Y%m%d_%H%M%S).log"

echo "========================================" | tee -a "$LOG_FILE"
echo "📊 週五總結週報 - $(date)" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"

# 啟動虛擬環境
if [ ! -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    echo "❌ 找不到虛擬環境" | tee -a "$LOG_FILE"
    exit 1
fi
source "$SCRIPT_DIR/venv/bin/activate"

# 產生總結週報並上傳
if python3 "$SCRIPT_DIR/generate_weekly_report.py" --type summary --upload 2>&1 | tee -a "$LOG_FILE"; then
    echo "" | tee -a "$LOG_FILE"
    echo "✅ 總結週報已產生並上傳到 ClickUp" | tee -a "$LOG_FILE"
else
    echo "" | tee -a "$LOG_FILE"
    echo "❌ 週報產生失敗" | tee -a "$LOG_FILE"
fi

echo "========================================" | tee -a "$LOG_FILE"

# 清理超過 30 天的舊日誌
find "$LOG_DIR" -name "friday_report_*.log" -mtime +30 -delete 2>/dev/null || true
