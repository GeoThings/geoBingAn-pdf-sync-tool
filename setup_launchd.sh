#!/bin/bash
#
# 安裝 macOS launchd 排程（取代 cron）
# launchd 在 Mac 從睡眠醒來時會自動補跑錯過的排程
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_DIR="$SCRIPT_DIR/launchd"

echo "🔧 安裝 geoBingAn launchd 排程"
echo "=========================================="
echo "專案目錄: $SCRIPT_DIR"
echo ""

# 確認
read -p "這會移除舊的 cron job 並安裝 launchd 排程。確認？(y/N) " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "取消"
    exit 0
fi

# 建立 logs 目錄
mkdir -p "$SCRIPT_DIR/logs"

# 1. 移除舊的 cron job
echo ""
echo "🗑️  移除舊的 cron job..."
crontab -l 2>/dev/null | grep -v "geoBingAn\|run_weekly_sync\|run_friday_report\|health_check" > /tmp/crontab_clean.txt 2>/dev/null || true
crontab /tmp/crontab_clean.txt 2>/dev/null || true
rm -f /tmp/crontab_clean.txt
echo "  ✅ cron job 已清除"

# 2. 卸載現有的 launchd job（如果有）
echo ""
echo "📦 安裝 launchd 排程..."
for plist in "$PLIST_DIR"/*.plist; do
    label=$(basename "$plist" .plist)
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
done

# 3. 複製 plist 到 LaunchAgents
mkdir -p "$LAUNCH_AGENTS_DIR"
for plist in "$PLIST_DIR"/*.plist; do
    cp "$plist" "$LAUNCH_AGENTS_DIR/"
    label=$(basename "$plist" .plist)
    launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENTS_DIR/$(basename $plist)"
    echo "  ✅ $label"
done

# 4. 驗證
echo ""
echo "📋 已安裝的排程："
echo "  每日 08:00  健康檢查（Token/磁碟/API）"
echo "  週一 09:00  完整同步 + 同步週報"
echo "  週五 18:00  總結週報"
echo ""
echo "⚡ 與 cron 的差異：Mac 從睡眠醒來時會自動補跑錯過的排程"
echo ""
echo "管理指令："
echo "  查看狀態: launchctl list | grep geobingan"
echo "  手動觸發: launchctl kickstart gui/$(id -u)/com.geothings.geobingan.weeklysync"
echo "  卸載全部: $SCRIPT_DIR/uninstall_launchd.sh"
echo "=========================================="
