#!/usr/bin/env bash
# 測試用的最小同步腳本：source 正式的 lib/watchdog.sh，模擬「主 shell ＋ 子程序」
# 與「cleanup 卡住」兩種情境。由 tests/test_watchdog.py 驅動。
set -e
set -o pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/../.." && pwd )"
LOG_DIR="${LOG_DIR:?need LOG_DIR}"
LOG_FILE="$LOG_DIR/harness.log"
MAX_RUNTIME_SECONDS="${MAX_RUNTIME_SECONDS:-3}"
TIMEOUT_GRACE_SECONDS="${TIMEOUT_GRACE_SECONDS:-3}"
TIMEOUT_MARKER="$LOG_DIR/.timeout.$$"
WAS_TIMEOUT=0
HAS_ERROR=0
ERROR_MESSAGE=""
CLEANUP_HANG_SECONDS="${CLEANUP_HANG_SECONDS:-0}"
WORK_SECONDS="${WORK_SECONDS:-1}"

source "$SCRIPT_DIR/lib/watchdog.sh"

cleanup() {
    local code=$?
    if [ -f "$TIMEOUT_MARKER" ]; then
        rm -f "$TIMEOUT_MARKER"; WAS_TIMEOUT=1; HAS_ERROR=1
        ERROR_MESSAGE="TIMEOUT"
    elif [ $code -ne 0 ] && [ $HAS_ERROR -eq 0 ]; then
        HAS_ERROR=1; ERROR_MESSAGE="UNEXPECTED:$code"
    fi
    echo "CLEANUP_START" >> "$LOG_DIR/events"
    [ "$CLEANUP_HANG_SECONDS" -gt 0 ] && sleep "$CLEANUP_HANG_SECONDS"
    echo "CLEANUP_DONE err=$HAS_ERROR msg=$ERROR_MESSAGE" >> "$LOG_DIR/events"
    # 正常路徑：子程序等同「python 跑完自己結束」，這裡替它收尾，
    # 免得測試留下 orphan。超時路徑不動——那是看門狗的職責，正是要驗的行為。
    if [ "${WAS_TIMEOUT}" -eq 0 ]; then
        [ -n "${CHILD_WRAPPER:-}" ] && wd_kill_tree "$CHILD_WRAPPER" TERM
        wd_stop
    fi
    exit $HAS_ERROR
}
trap cleanup EXIT
trap 'exit 1' TERM

wd_start
echo "$$" > "$LOG_DIR/main.pid"

# 模擬實際工作：一個長命的子程序（等同 python 掃 Drive）
bash -c 'echo $$ > "'"$LOG_DIR"'/child.pid"; sleep 120' > /dev/null 2>&1 &
CHILD_WRAPPER=$!
echo "$CHILD_WRAPPER" > "$LOG_DIR/wrapper.pid"
sleep "$WORK_SECONDS"
