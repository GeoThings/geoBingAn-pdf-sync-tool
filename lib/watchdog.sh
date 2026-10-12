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

#: 「醒著的秒數」。macOS 的 CLOCK_UPTIME_RAW 在系統睡眠期間**不前進**，而
#: `time.monotonic()` 在 macOS 就是它。實測這台（2026-10-12）：開機 240.94 小時、
#: CLOCK_UPTIME_RAW 只有 54.13 小時——睡掉 78%。CLOCK_MONOTONIC 不行，它和牆上
#: 時鐘一樣是 240.94 小時。
#:
#: 測試用 WD_AWAKE_CMD 注入假時鐘（真的讓機器睡覺沒辦法在測試裡做）。
#:
#: ⚠️ 結果放進 WD_AWAKE_NOW，**不走 stdout**。呼叫端一律寫
#: `wd_awake_now; x=$WD_AWAKE_NOW`，不可寫 `x=$(wd_awake_now)`：command
#: substitution 會起子 shell，函式裡設的 warn-once 旗標在子 shell 結束時就消失，
#: 於是降級警告每個輪詢間隔印一次——22 小時的牆上期限配 15 秒輪詢約 5,280 行，
#: 把日誌洗掉（#117 review P2）。
WD_AWAKE_NOW=0

wd_awake_now() {
    local out='' py
    if [ -n "${WD_AWAKE_CMD:-}" ]; then
        out="$($WD_AWAKE_CMD 2>/dev/null)" || out=''
    else
        for py in ${WD_PYTHON:-} "${SCRIPT_DIR:-.}/venv/bin/python" python3; do
            [ -n "$py" ] || continue
            [ -x "$py" ] || command -v "$py" >/dev/null 2>&1 || continue
            out="$("$py" -c 'import time; print(int(time.monotonic()))' 2>/dev/null)" \
                && [ -n "$out" ] && break
            out=''
        done
    fi
    case "$out" in
        ''|*[!0-9]*) ;;                 # 空的或非數字＝取不到（指令不存在、壞掉）
        *) WD_AWAKE_NOW="$out"; return 0 ;;
    esac
    # 取不到清醒時鐘就退回牆上時鐘——行為等於改版前，但**一定要出聲**。
    # 無聲降級的話，清醒預算會悄悄變成牆上預算，而那正是 10/09 誤殺的成因。
    # 只出聲一次：旗標設在當前 shell（不是子 shell）才留得住。
    if [ -z "${WD_AWAKE_WARNED:-}" ]; then
        WD_AWAKE_WARNED=1
        echo "⚠️  取不到「醒著的時間」時鐘，清醒預算退回牆上時鐘（睡眠期間也會計時）" \
            | tee -a "${LOG_FILE:-/dev/null}" >&2
    fi
    WD_AWAKE_NOW="$(date +%s)"
}

# 等到兩個期限之一到期：牆上時鐘 `wall_deadline`（epoch 秒）或清醒時鐘
# `awake_deadline`（wd_awake_now 的秒數，空字串＝不設）。到期時把成因寫進
# WD_EXPIRED_BY（wall／awake）。target 非空時，目標先結束就回 1。
#
# **為什麼是兩個期限而不是一個**（2026-10-12）：一個期限原本同時管兩件不同的事。
#  - 「這一輪卡住了」→ 要用**清醒時間**量。正常跑 15–17 分鐘，清醒超過 60 分才算卡住。
#  - 「不要壓到下一次排程」→ 要用**牆上時鐘**量，而基準是下次排程，不是開始後 60 分。
# 10/09 就是第二件事的門檻（60 分）誤判成第一件：機器睡了 81.6 分鐘，醒來時牆上
# 時鐘早就過期，而那一輪實際只跑了 0.4 分鐘。砍掉它符合當時的政策，但那個政策
# 把「睡過頭」當成「卡住」。
#
# ⚠️ 不可寫成 `sleep $((deadline - now))`：`sleep` 在**系統睡眠期間不前進**。
# 2026-10-06 實測：牆上 63.5 分鐘的執行裡，機器睡了 49.2 分鐘（6 段，最長 17 分），
# 實際清醒只有 14.3 分鐘——於是 `sleep 3600` 離到期還差得遠，看門狗完全沒觸發。
# 每輪順便檢查目標還活著，正常結束就提早離場。
# 單次 nap 取 min(poll, 兩邊剩餘)：既不超衝太多，也不會讓一次長 sleep 又把睡眠
# 問題帶回來。
wd_wait_until() {
    local wall_deadline=$1 awake_deadline=${2:-} target=${3:-}
    local now remain nap awake_remain
    WD_EXPIRED_BY=''
    while :; do
        now=$(date +%s)
        remain=$(( wall_deadline - now ))
        if [ "$remain" -le 0 ]; then
            WD_EXPIRED_BY='wall'
            return 0
        fi
        if [ -n "$awake_deadline" ]; then
            wd_awake_now                        # 不可用 $(...)，見 wd_awake_now
            awake_remain=$(( awake_deadline - WD_AWAKE_NOW ))
            if [ "$awake_remain" -le 0 ]; then
                WD_EXPIRED_BY='awake'
                return 0
            fi
            [ "$awake_remain" -lt "$remain" ] && remain="$awake_remain"
        fi
        if [ -n "$target" ] && ! kill -0 "$target" 2>/dev/null; then
            return 1                                    # 目標已結束
        fi
        nap="$WD_POLL_SECONDS"
        [ "$remain" -lt "$nap" ] && nap="$remain"
        sleep "$nap"
    done
}

#: 寬限期的牆上時鐘上限＝TIMEOUT_GRACE_SECONDS 的幾倍。寬限本身用清醒時鐘算
#: （睡著的機器不會把通知寄出去，醒來才算），但不能讓一台睡著的機器把殭屍程序
#: 無限期留著，所以再加一個牆上時鐘的絕對上限。
WD_GRACE_WALL_FACTOR="${WD_GRACE_WALL_FACTOR:-60}"

wd_start() {
    [ "${MAX_RUNTIME_SECONDS}" -le 0 ] && return 0      # 0 = 關閉（人工長跑用）
    local target=$$
    local wall_limit="${MAX_WALL_SECONDS:-0}"
    local awake_deadline wall_deadline
    wd_awake_now
    awake_deadline=$(( WD_AWAKE_NOW + MAX_RUNTIME_SECONDS ))
    if [ "$wall_limit" -gt 0 ]; then
        wall_deadline=$(( $(date +%s) + wall_limit ))
    else
        # 0 = 不設牆上上限。用一個極遠的期限而不是另寫一條分支：兩條路會走鐘。
        wall_deadline=$(( $(date +%s) + 315360000 ))
    fi
    (
        WD_SELF=$BASHPID          # 排除自己與自己的後代，否則會殺掉負責 SIGKILL 的自己
        wd_wait_until "$wall_deadline" "$awake_deadline" "$target" || exit 0
        kill -0 "$target" 2>/dev/null || exit 0
        local by="${WD_EXPIRED_BY:-unknown}"
        # 成因要寫進 marker：操作者看到告警時必須分得出「真的卡住」與「睡掉了
        # 整個執行窗」，那是兩個完全不同的處理方向。
        echo "TIMEOUT:${by}" > "$TIMEOUT_MARKER"
        {
            echo ""
            if [ "$by" = 'wall' ]; then
                echo "⏱️  已達牆上時間上限 ${wall_limit}s，強制終止（避免壓到下一次排程；"\
                     "機器若睡掉大半執行窗，清醒預算可能還沒用完）"
            else
                echo "⏱️  已達執行時間上限 ${MAX_RUNTIME_SECONDS}s（清醒計時），強制終止"
            fi
        } | tee -a "$LOG_FILE"
        wd_kill_tree "$target" TERM
        # 寬限要夠 cleanup 跑完 record_sync_result（要連網發告警信／ClickUp）。
        # 太短會在通知送出前就 SIGKILL，變成「超時了但沒人知道」。
        #
        # 寬限用**清醒時鐘**：機器睡著時什麼通知都寄不出去，牆上計時會讓 SIGKILL
        # 在醒來的那一刻立刻落下，cleanup 等於沒有寬限（和 10/09 同一個錯誤）。
        # 牆上那一側只當絕對上限，免得睡著的機器把殭屍留太久。
        wd_awake_now
        wd_wait_until \
            "$(( $(date +%s) + TIMEOUT_GRACE_SECONDS * WD_GRACE_WALL_FACTOR ))" \
            "$(( WD_AWAKE_NOW + TIMEOUT_GRACE_SECONDS ))"
        wd_kill_tree "$target" KILL
    ) &
    WATCHDOG_PID=$!
}

wd_stop() {
    [ -n "${WATCHDOG_PID:-}" ] || return 0
    wd_kill_tree "${WATCHDOG_PID}" TERM
    WATCHDOG_PID=''
}
