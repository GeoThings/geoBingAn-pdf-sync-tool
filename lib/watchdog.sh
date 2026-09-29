#!/usr/bin/env bash
# 整體執行時間看門狗。抽成獨立檔是為了能被測試 source（PR #98 review：
# 原本內嵌在 run_weekly_sync.sh 裡，只能靠抄一份邏輯去驗，抄的那份和正式的會走鐘）。
#
# 呼叫端需先設好：LOG_FILE、MAX_RUNTIME_SECONDS、TIMEOUT_GRACE_SECONDS、TIMEOUT_MARKER
#
# 兩個 review P1 的教訓寫在這裡，改動前請先讀：
#  1) 只殺主 shell 是不夠的。`kill -TERM $$` 不會動到 python／tee，它們會變成
#     orphan 繼續掃 Drive、繼續寫狀態檔，隔天照樣和新排程衝突——超時等於沒做。
#     所以要殺**整棵子程序樹**。
#  2) cleanup 不可在一開始就取消看門狗。取消掉的話，負責「寬限後 SIGKILL」的那個
#     程序就不存在了，宣稱的 grace deadline 是假的：cleanup 或通知卡住就沒人殺它。
#     → 超時路徑**不取消**；正常路徑在 cleanup **最後**才取消（讓 cleanup 本身
#       也受同一個期限保護）。

# 收集某 pid 的所有後代（深度優先）。必須在父程序還活著時收集完：
# 父死後子程序會被 reparent，`pgrep -P` 就再也找不到它們。
wd_descendants() {
    local pid=$1 kid
    for kid in $(pgrep -P "$pid" 2>/dev/null); do
        wd_descendants "$kid"
        echo "$kid"
    done
}

# 對 pid 與其所有後代送訊號。先收集完整清單再送，避免 reparent 造成漏殺。
wd_kill_tree() {
    local pid=$1 sig=$2 p
    local list
    list="$(wd_descendants "$pid") $pid"
    for p in $list; do
        [ -n "$p" ] && kill "-$sig" "$p" 2>/dev/null || true
    done
}

wd_start() {
    [ "${MAX_RUNTIME_SECONDS}" -le 0 ] && return 0      # 0 = 關閉（人工長跑用）
    local target=$$
    (
        sleep "${MAX_RUNTIME_SECONDS}"
        kill -0 "$target" 2>/dev/null || exit 0         # 已正常結束
        echo "TIMEOUT" > "$TIMEOUT_MARKER"
        {
            echo ""
            echo "⏱️  已達執行時間上限 ${MAX_RUNTIME_SECONDS}s，強制終止（避免壓到下一次排程）"
        } | tee -a "$LOG_FILE"
        wd_kill_tree "$target" TERM
        # 寬限要夠 cleanup 跑完 record_sync_result（要連網發告警信／ClickUp）。
        # 太短會在通知送出前就 SIGKILL，變成「超時了但沒人知道」。
        sleep "${TIMEOUT_GRACE_SECONDS}"
        wd_kill_tree "$target" KILL
    ) &
    WATCHDOG_PID=$!
}

wd_stop() {
    [ -n "${WATCHDOG_PID:-}" ] || return 0
    wd_kill_tree "${WATCHDOG_PID}" TERM
    WATCHDOG_PID=''
}
