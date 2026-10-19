#!/usr/bin/env python3
"""同步排程：哪些時刻「應該」有一輪，以及中間漏掉了幾輪。

2026-10-19 起**週末不排程**（`launchd/com.geothings.geobingan.weeklysync.plist`
的 `StartCalendarInterval` 改成 Weekday 1–5、10:00）。依據是 10/17、10/18 兩輪
各跑了 539.1／586.6 分鐘卻只同步到 9／10 份——機器在那個執行窗裡多半是睡著的，
耗電池、又把失敗率推高。

為什麼要有這個模組：健檢原本用「超過 48 小時沒更新」當作「漏跑超過一輪」的
**代理**，而那個代理只在每天都排程時成立。週末停排程後：

* 週五 10:00 跑完 → 週一 08:00 健檢是 70 小時，兩個 48 小時門檻會在**每個週一**
  誤報。永遠亮的告警等於沒有告警。
* 把門檻放寬到 96 小時也不對：週二故障要到週六才超過 96 小時，週間的漏跑反而
  變得看不見。兩個方向都是同一個後果——告警失去意義。

所以這裡不數小時，直接數「這段時間內**應該**跑過幾輪」。週間的語意與舊門檻完全
相同（容忍漏跑一輪＝48 小時內不告警），跨週末則自然不算異常，而且排程若再改
（例如改時間或改天數），只要改這裡的常數，兩個檢查會一起跟上。
"""
from datetime import datetime, time, timedelta

#: ISO 星期（1=一 … 7=日）。必須與 launchd plist 的 Weekday 一致——
#: 兩邊漂走就是「告警以為今天該跑、排程其實沒排」，測試會釘住這件事。
SYNC_WEEKDAYS = frozenset({1, 2, 3, 4, 5})
SYNC_HOUR = 10
SYNC_MINUTE = 0

#: 容忍漏跑幾輪。1＝「漏一輪還不算異常」，與舊的 48 小時門檻同義。
DEFAULT_TOLERATE_MISSED = 1


def scheduled_runs_between(start, end, *, weekdays=SYNC_WEEKDAYS,
                           hour=SYNC_HOUR, minute=SYNC_MINUTE):
    """`(start, end]` 之間排定的執行時刻有幾個。

    `start` 或 `end` 是 None 代表**無從判斷**，回 None——呼叫端不可把它當 0，
    那會把「不知道」說成「沒漏跑」，正是這套檢查要消滅的無聲綠燈。
    """
    if start is None or end is None:
        return None
    if end <= start:
        return 0
    run_time = time(hour=hour, minute=minute)
    count, day = 0, start.date()
    while day <= end.date():
        if day.isoweekday() in weekdays and start < datetime.combine(day, run_time) <= end:
            count += 1
        day += timedelta(days=1)
    return count


def missed_scheduled_runs(last_update, now, **kw):
    """自 `last_update` 之後、到 `now` 為止，應該有幾輪沒有反映進來。

    None＝無從判斷（沒有 `last_update`）。
    """
    return scheduled_runs_between(last_update, now, **kw)


def is_overdue(last_update, now, tolerate=DEFAULT_TOLERATE_MISSED, **kw):
    """漏跑是否已超過容忍值。

    無從判斷（None）**不算** overdue：那不是「沒問題」，而是要由呼叫端用自己的
    話回報「無從判斷」，不要被混進同一個燈號裡。
    """
    missed = missed_scheduled_runs(last_update, now, **kw)
    return missed is not None and missed > tolerate


def describe_gap(last_update, now, **kw):
    """給告警文字用：回 (漏跑輪數, 小時數)。小時數只是輔助說明，不是判準。"""
    missed = missed_scheduled_runs(last_update, now, **kw)
    hours = None if last_update is None else int((now - last_update).total_seconds() // 3600)
    return missed, hours

# ─────────────────────────────────────────────────────────────────────────────
# 「每 N 個日曆日一次」——兩個消費端共用同一條規則
#
# 為什麼不用「滿 N×24 小時」（`(now - last) >= timedelta(days=N)`）：兩個消費端的
# 時間戳都是**做完才寫**，而下一輪走到它的時刻每週會飄動幾十秒（取決於前面的步驟
# 跑多久）。於是「滿 168 小時」會變成每週擲一次硬幣：
#
#   · 2026-10-15：已標 404 的重驗差 **38 秒**沒到期，整批 14 案被跳過。
#   · 2026-10-19：dropbox 三包只抓到一包，另兩包差約 **10 秒**與 **47 秒**沒到期。
#
# 而且 2026-10-19 起週末不排程，跳過一輪的代價從 +1 天變成可能 +3 天（週五到期 →
# 週六不跑 → 下週一），7 天的節奏會變成 10 天。
#
# 需求是「每七個日曆日檢查一次」（Zhe 2026-10-19，`pdf-sync/Z-1019-4`），所以按
# **日期**比較：同一個星期幾固定重來，不受幾十秒飄動影響。各自減掉幾十秒只是把
# 硬幣換成一枚比較不容易翻的硬幣，不是修掉它。
# ─────────────────────────────────────────────────────────────────────────────


def calendar_days_since(last, now):
    """從 `last` 到 `now` 經過幾個**日曆日**。任一為 None 回 None（無從判斷）。"""
    if last is None or now is None:
        return None
    return (now.date() - last.date()).days


def is_due_by_calendar_days(last, now, days: int) -> bool:
    """每 `days` 個日曆日一次。

    `last` 是 None（沒有時間戳、或讀不出來）一律回 True——fail-open 倒向「做」這
    一側：髒資料不可以讓某一項永遠不更新（同 #100 失效資料夾重驗的紀律）。
    """
    elapsed = calendar_days_since(last, now)
    return True if elapsed is None else elapsed >= days
