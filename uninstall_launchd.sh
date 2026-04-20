#!/bin/bash
#
# 卸載 geoBingAn launchd 排程
#

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
UID_NUM=$(id -u)

echo "🗑️  卸載 geoBingAn launchd 排程"

for label in com.geothings.geobingan.healthcheck com.geothings.geobingan.weeklysync com.geothings.geobingan.fridayreport; do
    launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null && echo "  ✅ $label 已卸載" || echo "  ⏭️  $label 未安裝"
    rm -f "$LAUNCH_AGENTS_DIR/$label.plist"
done

echo "✅ 完成"
