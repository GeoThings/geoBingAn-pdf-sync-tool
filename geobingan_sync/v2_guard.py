"""v2（riskmap.today）的資料能不能信——一條規則，所有產物共用。

為什麼放在這裡而不是各入口（PR #125 review r2 的 P1）：我第一版把閘門加在
`run_weekly_sync.sh` 裡，結果漏了 `run_friday_report.sh`——那支週五 17:00 的
launchd job 自己呼叫 `generate_weekly_report --type summary --upload`，於是照樣
用停在關站前的 mapping 產出一份標著本週日期的週報並**自動上傳 ClickUp**。

漏掉的原因不是粗心，是結構：守門放在呼叫點，就得靠每個呼叫點自律，而呼叫點
會長出新的。所以改放在**產物自己的邊界**（兩個 CLI 入口），任何入口進來都會
被同一道規則擋住。

這些產物為什麼不能在 v2 不可用時產出：它們的數字來自
`permit_system_mapping.json` 與 `permit_registry.json`，而那兩個檔**只由步驟 2.5
與步驟 3 更新**——那兩步正是 v2 不可用時被跳過的。產出來會是「日期是本週、
內容卻沒量到」，而趨勢是前後兩份快照相減，於是顯示「+0」——「+0」和「沒量到」
分不出來。未取得 ≠ 0。
"""
import sys
from typing import Optional

# 「刻意跳過」要和「失敗」分得開，否則日誌會寫成「週報產生失敗」而誤導操作者。
SKIP_EXIT_CODE = 4

CARD = 'ClickUp z956jf7zrd'

# reason 的預設值不能用 None——None 要留給「明確沒有理由（健康）」，
# 否則測試注入不了健康那一面（這個 API 第一版就是這樣寫壞的）。
_COMPUTE = object()


def v2_unusable_reason(token: Optional[str] = None, now: Optional[float] = None) -> Optional[str]:
    """回 None＝v2 的資料可以信；回字串＝不能信，字串是理由。

    判準沿用 `check_refresh_token.classify`（同一條規則，不另寫一份）：
    `expired` 與 `unreadable` 都算不能信；`warning`（剩不到兩天）還能用。
    """
    from geobingan_sync.steps.check_refresh_token import classify
    if token is None:
        from geobingan_sync import config
        token = config.REFRESH_TOKEN
    state, _days, detail = classify(token, now=now)
    if state in ('expired', 'unreadable'):
        return f'riskmap token {state}（{detail}）；riskmap.today 即將關閉，見 {CARD}'
    return None


def cli_exit_code(result) -> int:
    """把 main() 的回傳值對映成 CLI 的結束碼。

    **CLI 的結束碼只能是結束碼。** 這條規則是我在 PR #125 裡違反過的：為了讓
    「刻意跳過」傳得出去，我把入口寫成 `sys.exit(main() or 0)`，但
    `weekly_snapshot.main()` 最後是 `return diff`——有前次快照時那是一個非空的
    dict，`sys.exit(dict)` 會把它印到 stderr 並以 exit 1 結束。快照其實寫成功了，
    結束碼卻說失敗（排程那行的 `|| true` 遮住了，但 dict 會被寫進日誌，
    而直接呼叫或用 `&&` 串接的人會誤判）。

    所以對映寫在這裡、所有入口共用：int 原樣傳遞（那是刻意給的結束碼），
    其他任何東西（None、dict、list…）都是**資料**，一律 0。
    `bool` 要先擋——它是 int 的子類，`True` 會變成 exit 1＝假的失敗。
    """
    if isinstance(result, bool):
        return 0
    if isinstance(result, int):
        return result
    return 0


def refuse_if_v2_unusable(what: str, reason=_COMPUTE, out=None) -> Optional[int]:
    """產物的邊界守門。

    回 None＝可以做下去；回 SKIP_EXIT_CODE＝不要做（理由已經印出來）。
    `reason` 可注入，測試不必依賴真實的 token。
    """
    out = out or sys.stdout
    reason = v2_unusable_reason() if reason is _COMPUTE else reason
    if not reason:
        return None
    print(f'⏭️  跳過{what}：{reason}', file=out)
    print(f'   數字來源（permit_system_mapping／permit_registry）這輪沒有更新，'
          f'產出來會是標著本週日期的舊資料。', file=out)
    print(f'   要恢復得先決定新的資料來源（見 {CARD}）。', file=out)
    return SKIP_EXIT_CODE
