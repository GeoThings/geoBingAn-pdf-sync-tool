#!/bin/bash
#
# 一鍵設定 Cron Job
# 磁碟修復後執行此腳本即可啟動全自動化
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "🔧 設定 geoBingAn 自動化排程"
echo "=========================================="
echo "專案目錄: $SCRIPT_DIR"
echo ""

# 確認
read -p "確認設定 3 個 cron job？(y/N) " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "取消"
    exit 0
fi

# 備份現有 crontab
crontab -l > /tmp/crontab_backup_$(date +%Y%m%d).txt 2>/dev/null || true

# 移除舊的 geoBingAn cron job
crontab -l 2>/dev/null | grep -v "geoBingAn\|run_weekly_sync\|run_friday_report\|health_check" > /tmp/crontab_clean.txt 2>/dev/null || true

# 加入新的 cron job
cat >> /tmp/crontab_clean.txt << CRON

# === geoBingAn 建案監測自動化 ===
# 每日 08:00 — 健康檢查（Token/磁碟/API）
0 8 * * * cd $SCRIPT_DIR && $SCRIPT_DIR/venv/bin/python3 $SCRIPT_DIR/health_check.py --notify >> $SCRIPT_DIR/logs/health_check.log 2>&1
# 週一 09:00 — 完整同步（sync + upload + match + report + weekly PDF）
0 9 * * 1 $SCRIPT_DIR/run_weekly_sync.sh
# 週五 18:00 — 總結週報 PDF
0 18 * * 5 $SCRIPT_DIR/run_friday_report.sh
CRON

crontab /tmp/crontab_clean.txt
rm /tmp/crontab_clean.txt

echo ""
echo "✅ Cron job 設定完成！"
echo ""
crontab -l | grep -A1 "geoBingAn"
echo ""
echo "=========================================="
echo "排程："
echo "  每日 08:00  健康檢查"
echo "  週一 09:00  完整同步 + 同步週報"
echo "  週五 18:00  總結週報"
