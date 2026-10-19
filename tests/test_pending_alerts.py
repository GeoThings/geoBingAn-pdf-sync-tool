"""送不出去的告警要留著並補送，不可以丟掉。

2026-10-19 08:00:35 那輪健檢（post-sleep）DNS 全解不到，9 個要關注的問題一個都沒
送出。原有的 plan→send→commit 只做對一半：不落地狀態、下一輪會重新產生事件——
但那只在**問題還在**的時候有效。問題若在下一輪之前消失，事件會變成 resolved，
原本那則就永遠沒送到人手上。

這裡釘住兩邊：訊息不會丟（含逾時、佇列滿、安靜的一天），以及**不會重複送**。
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import health_check
from geobingan_sync.alert_state import AlertState
from geobingan_sync.pending_alerts import PendingAlerts

T0 = datetime(2026, 10, 19, 8, 0)


def _q(tmp_path, **kw):
    return PendingAlerts(path=tmp_path / 'pending.json', namespace='test', **kw)


class _Send:
    """可控的發送器：記下每次呼叫，依 ok 決定成功或失敗。"""

    def __init__(self, ok=True, raises=None):
        self.ok, self.raises, self.calls = ok, raises, []

    def __call__(self, title, body, needs_attention):
        self.calls.append((title, body, needs_attention))
        if self.raises:
            raise self.raises
        return self.ok


# ---------- 入列與補送 ----------

def test_an_undeliverable_alert_is_kept(tmp_path):
    q = _q(tmp_path)
    q.add('標題', '內文', True, now=T0, error='NameResolutionError')
    items = q.load()
    assert len(items) == 1
    assert items[0]['title'] == '標題' and items[0]['attempts'] == 0
    assert 'NameResolutionError' in items[0]['last_error']


def test_a_flushed_alert_is_removed_only_when_the_send_succeeds(tmp_path):
    q = _q(tmp_path)
    q.add('標題', '內文', True, now=T0)
    bad = _Send(ok=False)
    sent, remaining = q.flush(bad, now=T0 + timedelta(hours=1))
    assert (sent, remaining) == (0, 1), '送不出去就不可以移除'
    assert q.load()[0]['attempts'] == 1

    good = _Send(ok=True)
    sent, remaining = q.flush(good, now=T0 + timedelta(hours=2))
    assert (sent, remaining) == (1, 0)
    assert q.load() == []


def test_a_raising_send_does_not_lose_the_item(tmp_path):
    q = _q(tmp_path)
    q.add('標題', '內文', False, now=T0)
    sent, remaining = q.flush(_Send(raises=OSError('boom')), now=T0)
    assert (sent, remaining) == (0, 1)
    assert 'OSError' in q.load()[0]['last_error']


def test_the_backlog_goes_out_oldest_first(tmp_path):
    q = _q(tmp_path)
    q.add('第二則', 'b', False, now=T0 + timedelta(hours=2))
    q.add('第一則', 'a', False, now=T0)
    good = _Send(ok=True)
    q.flush(good, now=T0 + timedelta(hours=3))
    assert ['第一則' in c[0] for c in good.calls] == [True, False]


def test_a_resent_alert_says_when_it_was_originally_raised(tmp_path):
    """補送的文字要標明原本的時間，否則讀者會以為是現在發生的。"""
    q = _q(tmp_path)
    q.add('❌ Token 已過期', '詳細內容', True, now=T0)
    good = _Send(ok=True)
    q.flush(good, now=T0 + timedelta(hours=5))
    title, body, needs_attention = good.calls[0]
    assert '補送' in title and '2026-10-19 08:00' in title
    assert '不是現在的' in body and '詳細內容' in body
    assert needs_attention is True


def test_one_dead_channel_is_not_retried_once_per_item(tmp_path):
    """通道不通時不用把每一則都各打一次——那只是把同一個失敗乘上佇列長度。"""
    q = _q(tmp_path)
    for i in range(4):
        q.add(f'第 {i} 則', 'x', False, now=T0 + timedelta(minutes=i))
    bad = _Send(ok=False)
    sent, remaining = q.flush(bad, now=T0 + timedelta(hours=1))
    assert (sent, remaining) == (0, 4)
    assert len(bad.calls) == 1, '只該試第一則就停手'


# ---------- 有限重試，但不丟訊息 ----------

def test_retries_stop_after_the_limit_but_the_message_stays(tmp_path):
    q = _q(tmp_path, max_attempts=3)
    q.add('標題', '內文', True, now=T0)
    bad = _Send(ok=False)
    for i in range(5):
        q.flush(bad, now=T0 + timedelta(hours=i + 1))
    items = q.load()
    assert len(items) == 1, '逾時不可以把訊息丟掉'
    assert items[0]['attempts'] == 3, '達上限後就不再自動重打'
    assert items[0]['stalled'] is True
    assert len(bad.calls) == 3


def test_a_stalled_item_is_reported_as_error(tmp_path):
    q = _q(tmp_path, max_attempts=1)
    q.add('標題', '內文', True, now=T0)
    q.flush(_Send(ok=False), now=T0 + timedelta(hours=1))
    level, msg = health_check.check_pending_alerts(pending=q, now=T0 + timedelta(hours=2))
    assert level == 'error' and '停止自動重打' in msg and '仍保留' in msg


# ---------- 佇列滿了也不丟 ----------

def test_an_overflowing_queue_coalesces_instead_of_dropping(tmp_path):
    q = _q(tmp_path, max_items=3)
    for i in range(5):
        q.add(f'第 {i} 則', 'x', i == 0, now=T0 + timedelta(minutes=i))
    items = q.load()
    assert len(items) == 3
    summary = items[0]
    assert summary['coalesced'] == 3 and '已合併' in summary['title']
    # 「合併的合併」也要把則數累加、標題也要跟著進去，否則越合併越像沒發生過
    assert '第 0 則' in summary['body'] and '第 2 則' in summary['body']
    assert '3 則' in summary['title']
    assert summary['needs_attention'] is True, '被合併的那批裡有要打擾的，摘要就要打擾'
    assert q.summary(now=T0)['coalesced'] == 3


def test_the_summary_counts_what_is_still_waiting(tmp_path):
    q = _q(tmp_path)
    assert q.summary(now=T0) == {'count': 0, 'oldest_age_hours': None, 'stalled': 0, 'coalesced': 0}
    q.add('a', 'x', False, now=T0)
    info = q.summary(now=T0 + timedelta(hours=6))
    assert info['count'] == 1 and round(info['oldest_age_hours']) == 6


def test_a_corrupt_queue_file_is_not_a_crash(tmp_path):
    p = tmp_path / 'pending.json'
    p.write_text('{ not json', encoding='utf-8')
    q = PendingAlerts(path=p, namespace='test')
    assert q.load() == []


# ---------- 與 AlertState 的接線 ----------

def _state(tmp_path):
    return AlertState(path=tmp_path / 'alert_state.json', namespace='healthcheck')


def test_a_failed_health_check_alert_lands_in_the_queue(tmp_path):
    st = _state(tmp_path)
    events, delivered = st.process({'API 連線': ('error', 'DNS 解不到')},
                                   send=_Send(ok=False), now=T0)
    assert delivered is False and len(events) == 1
    assert len(st.pending.load()) == 1, '沒送出去的通知必須留著'
    assert st.load() == {}, '狀態不落地，下輪才會重新產生事件'


def test_a_delivered_alert_is_not_queued(tmp_path):
    """反方向：送達了就不該入列，否則人會收到兩次。"""
    st = _state(tmp_path)
    st.process({'API 連線': ('error', 'DNS 解不到')}, send=_Send(ok=True), now=T0)
    assert st.pending.load() == []


def test_the_backlog_is_sent_on_the_next_round(tmp_path):
    """下一輪是**別的**問題：積壓那則沒被涵蓋，所以要跟著補送出去。

    （問題還在、事件重新產生的情況反而不該補送——那是重複通知，見
    `test_a_requeued_alert_is_not_sent_twice_when_the_problem_persists`。）
    """
    st = _state(tmp_path)
    st.process({'API 連線': ('error', 'DNS 解不到')}, send=_Send(ok=False), now=T0)
    good = _Send(ok=True)
    st.process({'磁碟空間': ('warning', '只剩 2.6 GB')}, send=good, now=T0 + timedelta(days=1))
    assert st.pending.load() == []
    assert any('補送' in c[0] for c in good.calls), '先前沒送出去的那一則要補送'
    assert any('磁碟空間' in c[1] for c in good.calls), '本輪的新問題也要送'


def test_a_quiet_day_still_drains_the_backlog(tmp_path):
    """問題消失的那一天最不該讓舊訊息繼續躺著——而那天不會產生任何事件。"""
    st = _state(tmp_path)
    st.process({'API 連線': ('error', 'DNS 解不到')}, send=_Send(ok=False), now=T0)
    good = _Send(ok=True)
    events, delivered = st.process({}, send=good, now=T0 + timedelta(days=1))
    assert events == [] and delivered is True
    assert st.pending.load() == [], '安靜的一天也要把積壓送出去'
    assert len(good.calls) == 1 and '補送' in good.calls[0][0]


def test_an_empty_queue_does_not_call_send_on_a_quiet_day(tmp_path):
    st = _state(tmp_path)
    quiet = _Send(ok=True)
    st.process({}, send=quiet, now=T0)
    assert quiet.calls == [], '沒事就不要發東西'


def test_the_queue_follows_the_state_file_into_tmp(tmp_path):
    """測試注入 tmp 路徑時，佇列也要在 tmp——不可以寫到正式的 state/。"""
    st = _state(tmp_path)
    assert st.pending.path.parent == tmp_path


# ---------- 先等 DNS，有上限 ----------

def test_the_health_check_waits_for_dns_before_checking(tmp_path, monkeypatch):
    calls = []

    def fake_wait(hosts, timeout=0):
        calls.append((tuple(hosts), timeout))
        return True, []

    health_check.run_health_check(checks=[('假檢查', lambda: ('ok', '好'))],
                                  notify=True, alert_state=_state(tmp_path),
                                  now=T0, send=_Send(ok=True), network_wait=fake_wait)
    assert len(calls) == 1 and calls[0][1] > 0, '要等、而且要有上限'


def test_a_dns_timeout_does_not_abort_the_health_check(tmp_path):
    ready, unresolved = health_check.wait_for_alert_network(
        timeout=1.0, hosts=['nope.invalid'], wait=lambda hosts, timeout=0: (False, list(hosts)))
    assert ready is False and unresolved == ['nope.invalid']


def test_a_dry_run_does_not_wait(tmp_path):
    """手動乾跑不要被網路等待卡住。"""
    calls = []
    health_check.run_health_check(checks=[('假檢查', lambda: ('ok', '好'))],
                                  notify=False, alert_state=_state(tmp_path), now=T0,
                                  network_wait=lambda hosts, timeout=0: calls.append(1) or (True, []))
    assert calls == []


def test_alert_hosts_come_from_config_not_a_hardcoded_list():
    hosts = health_check.alert_hosts()
    assert hosts, '至少要有 API 的 host'
    assert all(isinstance(h, str) and h for h in hosts)
    assert len(hosts) == len(set(hosts)), '不要重複'


# ---------- 不要重複通知 ----------

def test_a_requeued_alert_is_not_sent_twice_when_the_problem_persists(tmp_path):
    """問題還在：下一輪事件會重新產生並講一次現況，積壓那則就是同一件事的舊版本。"""
    st = _state(tmp_path)
    st.process({'API 連線': ('error', 'DNS 解不到')}, send=_Send(ok=False), now=T0)
    good = _Send(ok=True)
    st.process({'API 連線': ('error', 'DNS 解不到')}, send=good, now=T0 + timedelta(days=1))
    assert len(good.calls) == 1, '同一件事不可以通知兩次'
    assert '補送' not in good.calls[0][0]
    assert st.pending.load() == []


def test_a_partially_covered_backlog_item_is_kept(tmp_path):
    """積壓那則講了 A 和 B，新通知只講了 A → B 還沒送到，整則留著補送。"""
    st = _state(tmp_path)
    st.process({'A': ('error', 'x'), 'B': ('warning', 'y')}, send=_Send(ok=False), now=T0)
    good = _Send(ok=True)
    st.process({'A': ('error', 'x')}, send=good, now=T0 + timedelta(days=1))
    assert any('補送' in c[0] for c in good.calls), 'B 的資訊還沒送到，不可以丟'


def test_an_item_without_keys_is_never_dropped_as_covered(tmp_path):
    """舊格式沒記 key：判斷不了就不丟。"""
    q = _q(tmp_path)
    item = q.add('舊格式', 'x', False, now=T0)
    items = q.load()
    items[0].pop('keys')
    q.save(items)
    assert q.resolve_covered(['任何 key'], now=T0) == 0
    assert len(q.load()) == 1
