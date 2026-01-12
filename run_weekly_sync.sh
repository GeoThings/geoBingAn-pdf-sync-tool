#!/bin/bash
#
# geoBingAn PDF 週期同步執行腳本
# 用途：每週執行 PDF 同步和上傳流程
#
# 執行順序：
# 1. sync_permits.py - 從台北市政府網站同步最新建案 PDF 到 Google Drive
# 2. upload_pdfs.py - 上傳最近 7 天更新的 PDF 到 geoBingAn Backend
# 3. generate_permit_tracking_report.py - 生成建照監測追蹤報告
#

set -e  # 遇到錯誤立即退出

# 切換到腳本所在目錄
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# 日誌目錄
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

# 日誌檔案（使用日期時間命名）
LOG_FILE="$LOG_DIR/weekly_sync_$(date +%Y%m%d_%H%M%S).log"

echo "========================================" | tee -a "$LOG_FILE"
echo "🚀 開始執行週期同步 - $(date)" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"

# 啟動虛擬環境
source "$SCRIPT_DIR/venv/bin/activate"

# 步驟 1: 同步 PDF 從台北市政府到 Google Drive
echo "" | tee -a "$LOG_FILE"
echo "📥 步驟 1/3: 同步 PDF 從台北市政府網站..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
python3 "$SCRIPT_DIR/sync_permits.py" 2>&1 | tee -a "$LOG_FILE"

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
echo "📤 步驟 2/3: 上傳最近 7 天的 PDF 到 Backend..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
python3 "$SCRIPT_DIR/upload_pdfs.py" 2>&1 | tee -a "$LOG_FILE"

# 步驟 3: 生成建照監測追蹤報告
echo "" | tee -a "$LOG_FILE"
echo "📊 步驟 3/3: 生成建照監測追蹤報告..." | tee -a "$LOG_FILE"
echo "----------------------------------------" | tee -a "$LOG_FILE"
python3 "$SCRIPT_DIR/generate_permit_tracking_report.py" 2>&1 | tee -a "$LOG_FILE"

# 完成
echo "" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"
echo "✅ 週期同步執行完成 - $(date)" | tee -a "$LOG_FILE"
echo "========================================" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

# 清理超過 30 天的舊日誌
find "$LOG_DIR" -name "weekly_sync_*.log" -mtime +30 -delete

exit 0
