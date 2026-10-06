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
#
# ⚠️ 必須排除自己：看門狗子程序本身就是主 shell 的後代，不排除的話它會把自己
# 一起殺掉，「寬限後 SIGKILL」那一段就永遠不會執行——grace 又變成假的。
# 這個 bug 在 macOS 本機「通過」了、在 Linux CI 才紅：純粹是誰先被殺到的時序差異，
# 屬於環境相依的假綠，比直接壞掉更危險。呼叫端用 WD_SELF 指定要排除的 pid。
wd_kill_tree() {
    local pid=$1 sig=$2 p
    local skip=""
    if [ -n "${WD_SELF:-}" ]; then
        skip="${WD_SELF} $(wd_descendants "${WD_SELF}")"
    fi
    local list
    list="$(wd_descendants "$pid") $pid"
    for p in $list; do
        [ -n "$p" ] || continue
        case " $skip " in *" $p "*) continue ;; esac
        kill "-$sig" "$p" 2>/dev/null || true
    done
}

#: 輪詢間隔（秒）。用輪詢而不是一次 sleep 到期，理由見 wd_wait_until。
WD_POLL_SECONDS="${WD_POLL_SECONDS:-15}"

# 等到**牆上時鐘**到達 deadline（epoch 秒）。target 非空時，目標先結束就回 1。
#
# ⚠️ 不可寫成 `sleep $((deadline - now))`：`sleep` 在**系統睡眠期間不前進**。
# 2026-10-06 實測：牆上 63.5 分鐘的執行裡，機器睡了 49.2 分鐘（6 段，最長 17 分），
# 實際清醒只有 14.3 分鐘——於是 `sleep 3600` 離到期還差得遠，看門狗完全沒觸發，
# 而 START_TIME 用的是 `date +%s`（牆上時鐘）。**兩個時鐘不同，宣稱的上限是假的。**
# 每輪順便檢查目標還活著，正常結束就提早離場。
# 單次 nap 取 min(poll, 剩餘)：既不超衝太多，也不會讓一次長 sleep 又把睡眠問題
# 帶回來。
wd_wait_until() {
    local deadline=$1 target=${2:-} now remain nap
    while :; do
        now=$(date +%s)
        remain=$(( deadline - now ))
        [ "$remain" -le 0 ] && return 0
        if [ -n "$target" ] && ! kill -0 "$target" 2>/dev/null; then
            return 1                                    # 目標已結束
        fi
        nap="$WD_POLL_SECONDS"
        [ "$remain" -lt "$nap" ] && nap="$remain"
        sleep "$nap"
    done
}

wd_start() {
    [ "${MAX_RUNTIME_SECONDS}" -le 0 ] && return 0      # 0 = 關閉（人工長跑用）
    local target=$$
    local deadline=$(( $(date +%s) + MAX_RUNTIME_SECONDS ))
    (
        WD_SELF=$BASHPID          # 排除自己與自己的後代，否則會殺掉負責 SIGKILL 的自己
        wd_wait_until "$deadline" "$target" || exit 0    # 已正常結束
        kill -0 "$target" 2>/dev/null || exit 0
        echo "TIMEOUT" > "$TIMEOUT_MARKER"
        {
            echo ""
            echo "⏱️  已達執行時間上限 ${MAX_RUNTIME_SECONDS}s，強制終止（避免壓到下一次排程）"
        } | tee -a "$LOG_FILE"
        wd_kill_tree "$target" TERM
        # 寬限要夠 cleanup 跑完 record_sync_result（要連網發告警信／ClickUp）。
        # 太短會在通知送出前就 SIGKILL，變成「超時了但沒人知道」。
        # 這裡同樣走牆上時鐘：睡眠中的 sleep 會把 SIGKILL 無限延後。
        wd_wait_until "$(( $(date +%s) + TIMEOUT_GRACE_SECONDS ))"
        wd_kill_tree "$target" KILL
    ) &
    WATCHDOG_PID=$!
}

wd_stop() {
    [ -n "${WATCHDOG_PID:-}" ] || return 0
    wd_kill_tree "${WATCHDOG_PID}" TERM
    WATCHDOG_PID=''
}
