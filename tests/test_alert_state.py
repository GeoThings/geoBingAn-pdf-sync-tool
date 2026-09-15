"""alert_state.plan_alerts：去重 / 升級 / 定期提醒 / 解除（批次 A2）。"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.alert_state import plan_alerts, format_events, AlertState

T0 = datetime(2026, 9, 15, 8, 0)


def _kinds(events):
    return [(e.kind, e.key) for e in events]


def test_new_issue_sends_and_is_recorded():
    events, state = plan_alerts({}, {'上傳暫停': ('warning', '已暫停 1 天')}, T0)
    assert _kinds(events) == [('new', '上傳暫停')]
    assert state['上傳暫停']['first_seen'] == T0.isoformat()
    assert state['上傳暫停']['last_sent'] == T0.isoformat()


def test_same_issue_next_day_is_suppressed():
    _, state = plan_alerts({}, {'上傳暫停': ('warning', '已暫停 1 天')}, T0)
    events, state2 = plan_alerts(state, {'上傳暫停': ('warning', '已暫停 2 天')}, T0 + timedelta(days=1))
    assert events == []                       # 不再洗版
    assert state2['上傳暫停']['first_seen'] == T0.isoformat()   # 起始時間保留
    assert state2['上傳暫停']['message'] == '已暫停 2 天'        # 訊息更新


def test_escalation_warning_to_error_sends():
    _, state = plan_alerts({}, {'JWT Token': ('warning', '剩 4 天')}, T0)
    events, _ = plan_alerts(state, {'JWT Token': ('error', '已過期')}, T0 + timedelta(days=5))
    assert _kinds(events) == [('escalated', 'JWT Token')]


def test_reminder_after_seven_days():
    _, state = plan_alerts({}, {'上傳暫停': ('warning', 'x')}, T0)
    events, _ = plan_alerts(state, {'上傳暫停': ('warning', 'x')}, T0 + timedelta(days=6, hours=23))
    assert events == []
    events, state2 = plan_alerts(state, {'上傳暫停': ('warning', 'x')}, T0 + timedelta(days=7))
    assert _kinds(events) == [('reminder', '上傳暫停')]
    assert state2['上傳暫停']['last_sent'] == (T0 + timedelta(days=7)).isoformat()


def test_resolved_emits_once_and_drops_key():
    _, state = plan_alerts({}, {'JWT Token': ('error', '已過期')}, T0)
    events, state2 = plan_alerts(state, {}, T0 + timedelta(days=1))
    assert _kinds(events) == [('resolved', 'JWT Token')]
    assert state2 == {}
    events3, _ = plan_alerts(state2, {}, T0 + timedelta(days=2))
    assert events3 == []                      # 恢復只發一次


def test_format_mentions_only_on_error():
    events, _ = plan_alerts({}, {'上傳暫停': ('warning', 'a'), 'JWT Token': ('error', 'b')}, T0)
    title, body, mention = format_events(events, T0)
    assert mention is True and '❌' in title
    events, _ = plan_alerts({}, {'上傳暫停': ('warning', 'a')}, T0)
    _, _, mention = format_events(events, T0)
    assert mention is False
    events, _ = plan_alerts({'X': {'level': 'error', 'message': 'm', 'last_sent': T0.isoformat()}}, {}, T0)
    title, body, mention = format_events(events, T0)
    assert mention is False and body.startswith('✅ 已恢復')


def test_alert_state_persists_roundtrip(tmp_path):
    st = AlertState(tmp_path / 'alert_state.json')
    assert st.process({'k': ('warning', 'm')}, now=T0) and (tmp_path / 'alert_state.json').exists()
    assert st.process({'k': ('warning', 'm')}, now=T0 + timedelta(days=1)) == []   # 從檔案讀回、抑制
    assert [e.kind for e in st.process({}, now=T0 + timedelta(days=2))] == ['resolved']
