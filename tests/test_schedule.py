#!/usr/bin/env python3
"""排程日模型：週末停排程後，「漏跑」不能再用小時數當代理。

2026-10-19 起 weeklysync 改成週一至週五。健檢原本兩處用 48 小時判斷「漏跑超過
一輪」，週五 10:00 → 週一 08:00 是 70 小時，會在每個週一誤報一次。這裡釘住兩件
事：預期的休息日不算異常，而**週間的敏感度一點都沒有放寬**（後者才是容易在修
誤報時被犧牲掉的那一半）。

日期全部寫死且標出星期，避免「今天剛好是週幾」決定測試會不會過。
"""
import plistlib
from datetime import datetime
from pathlib import Path

import pytest

import health_check
from geobingan_sync import schedule
from geobingan_sync.sync_status import weekday_summary

# 2026-10-19 是週一（實測 `last reboot` 顯示「一 10月 19」）
MON = datetime(2026, 10, 19, 10, 0)      # 週一 10:00（排程時刻）
FRI_PREV = datetime(2026, 10, 16, 10, 7)  # 前一個週五，剛跑完寫下的時間戳
SAT = datetime(2026, 10, 17, 10, 0)
SUN = datetime(2026, 10, 18, 10, 0)


def test_the_weekend_has_no_scheduled_runs():
    assert schedule.scheduled_runs_between(
        datetime(2026, 10, 16, 23, 0), datetime(2026, 10, 19, 9, 0)) == 0


def test_a_monday_morning_does_not_count_the_weekend_as_missed():
    """核心：週一 08:00 健檢看到週五的時間戳，不算漏跑（舊門檻會算 70 小時）。"""
    monday_check = datetime(2026, 10, 19, 8, 0)
    assert schedule.missed_scheduled_runs(FRI_PREV, monday_check) == 0
    assert schedule.is_overdue(FRI_PREV, monday_check) is False


def test_the_old_hour_threshold_would_have_fired_on_that_monday():
    """對照：同一組輸入在舊的 48 小時代理下是 70 小時，所以會誤報。"""
    monday_check = datetime(2026, 10, 19, 8, 0)
    hours = (monday_check - FRI_PREV).total_seconds() / 3600
    assert hours > 48 and round(hours) == 70


def test_one_missed_weekday_run_is_tolerated():
    """週二 10:30 看到週一的結算＝漏一輪，與舊門檻同義（48 小時內不報）。"""
    assert schedule.is_overdue(datetime(2026, 10, 19, 10, 7),
                               datetime(2026, 10, 20, 10, 30)) is False


def test_two_missed_weekday_runs_still_alarm():
    """週三 10:30 看到週一的結算＝漏兩輪，照舊告警——週間敏感度沒有被放寬。"""
    assert schedule.is_overdue(datetime(2026, 10, 19, 10, 7),
                               datetime(2026, 10, 21, 10, 30)) is True


def test_a_friday_failure_is_caught_on_tuesday_not_only_on_saturday():
    """若改用放寬到 96 小時的做法，週五故障要等到週二才超過門檻；這裡要更早。

    週五跑完後再也沒更新：週一漏一輪（容忍），週二就是漏兩輪＝告警。
    """
    assert schedule.is_overdue(FRI_PREV, datetime(2026, 10, 20, 10, 30)) is True


def test_no_last_update_is_unknown_not_zero():
    """沒有時間戳＝無從判斷，不可當成「沒漏跑」。"""
    assert schedule.missed_scheduled_runs(None, MON) is None
    assert schedule.is_overdue(None, MON) is False  # 交呼叫端用自己的話回報


def test_a_timestamp_in_the_future_is_not_a_gap():
    assert schedule.scheduled_runs_between(MON, FRI_PREV) == 0


def test_the_installed_weekdays_match_the_plist():
    """常數與 plist 的 Weekday 必須一致，否則告警與排程會各說各話。"""
    data = plistlib.loads(
        (Path(__file__).resolve().parents[1]
         / 'launchd' / 'com.geothings.geobingan.weeklysync.plist').read_bytes())
    intervals = data['StartCalendarInterval']
    assert isinstance(intervals, list), 'plist 仍是單一 dict＝每天都排，與週末停排程矛盾'
    assert {i['Weekday'] for i in intervals} == set(schedule.SYNC_WEEKDAYS)
    assert {i['Hour'] for i in intervals} == {schedule.SYNC_HOUR}
    assert {i.get('Minute', 0) for i in intervals} == {schedule.SYNC_MINUTE}


# ---------- 健檢的兩個消費端 ----------

def _unsupported(tmp_path, generated_at):
    """沿用 test_unsupported_sources 的測資形狀：entries 是 (建照, 連結, 註記) tuple。"""
    from geobingan_sync import unsupported_sources as us
    entries = [('111建字第0001號', 'https://cectw-my.sharepoint.com/:f:/g/x/E1', 'no_drive_link')]
    p = tmp_path / 'unsupported_sources.json'
    us.save(us.build(entries, now=generated_at), str(p))
    return str(p)


def _sync_status(tmp_path, last_run, history=None):
    import json
    p = tmp_path / 'sync_status.json'
    p.write_text(json.dumps({'last_run': last_run.isoformat(), 'last_status': 'success',
                             'history': history or []}), encoding='utf-8')
    return str(p)


def test_a_friday_list_is_not_stale_on_monday(tmp_path):
    level, msg = health_check.check_unsupported_sources(
        path=_unsupported(tmp_path, FRI_PREV), now=datetime(2026, 10, 19, 8, 0),
        sync_status_path=_sync_status(tmp_path, FRI_PREV))
    assert '未更新' not in msg, msg


def test_a_list_that_missed_two_weekday_runs_is_stale(tmp_path):
    level, msg = health_check.check_unsupported_sources(
        path=_unsupported(tmp_path, FRI_PREV), now=datetime(2026, 10, 20, 10, 30),
        sync_status_path=_sync_status(tmp_path, datetime(2026, 10, 20, 10, 5)))
    assert level == 'warning' and '漏 2 輪未更新' in msg, msg


# ---------- 只看排程日的統計 ----------

def test_weekday_summary_leaves_the_weekend_rounds_out():
    history = [
        {'date': '2026-10-16', 'status': 'success'},   # 五
        {'date': '2026-10-17', 'status': 'failure'},   # 六
        {'date': '2026-10-18', 'status': 'failure'},   # 日
        {'date': '2026-10-19', 'status': 'partial'},   # 一
    ]
    assert weekday_summary(history) == {'runs': 2, 'success': 1, 'other': 1}


def test_weekday_summary_does_not_rewrite_history():
    history = [{'date': '2026-10-17', 'status': 'failure'}]
    before = [dict(h) for h in history]
    weekday_summary(history)
    assert history == before


def test_weekday_summary_skips_unreadable_dates_instead_of_guessing():
    history = [{'date': 'n/a', 'status': 'success'}, {'status': 'success'},
               {'date': '2026-10-19', 'status': 'success'}]
    assert weekday_summary(history) == {'runs': 1, 'success': 1, 'other': 0}


@pytest.mark.parametrize('day,expected', [(SAT, 0), (SUN, 0), (MON, 1)])
def test_only_scheduled_days_produce_a_run_time(day, expected):
    start = day.replace(hour=0, minute=0)
    end = day.replace(hour=23, minute=59)
    assert schedule.scheduled_runs_between(start, end) == expected


def test_the_run_that_wrote_the_timestamp_is_not_counted_as_missed():
    """時間戳正好是排程時刻時，那一輪是「寫下它的那一輪」，不是漏掉的一輪。

    區間要是 (start, end]；寫成含左端就會把自己算成漏跑，第一次檢查就誤報。
    """
    assert schedule.scheduled_runs_between(
        datetime(2026, 10, 19, 10, 0), datetime(2026, 10, 19, 23, 0)) == 0
