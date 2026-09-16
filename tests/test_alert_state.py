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
    # error 恢復：仍要送到人，否則承諾的「恢復通知」等於沒有（review P2）
    events, _ = plan_alerts({'X': {'level': 'error', 'message': 'm', 'last_sent': T0.isoformat()}}, {}, T0)
    title, body, attention = format_events(events, T0)
    assert attention is True and body.startswith('✅ 已恢復')
    # warning 恢復：純紀錄，不打擾
    events, _ = plan_alerts({'Y': {'level': 'warning', 'message': 'm', 'last_sent': T0.isoformat()}}, {}, T0)
    _, body, attention = format_events(events, T0)
    assert attention is False and body.startswith('✅ 已恢復')


OK = lambda t, b, m: True
FAIL = lambda t, b, m: False


def test_alert_state_persists_roundtrip(tmp_path):
    st = AlertState(tmp_path / 'alert_state.json')
    ev, ok = st.process({'k': ('warning', 'm')}, send=OK, now=T0)
    assert ev and ok and (tmp_path / 'alert_state.json').exists()
    assert st.process({'k': ('warning', 'm')}, send=OK, now=T0 + timedelta(days=1))[0] == []   # 讀回、抑制
    assert [e.kind for e in st.process({}, send=OK, now=T0 + timedelta(days=2))[0]] == ['resolved']


def test_send_failure_keeps_state_and_retries_next_run(tmp_path):
    """review P1：發送失敗不可寫 last_sent，否則會被壓 7 天。"""
    st = AlertState(tmp_path / 's.json')
    ev, ok = st.process({'k': ('error', 'm')}, send=FAIL, now=T0)
    assert [e.kind for e in ev] == ['new'] and ok is False
    assert not (tmp_path / 's.json').exists()                       # 舊狀態（空）保留
    def boom(t, b, m): raise RuntimeError('clickup 500')
    ev, ok = st.process({'k': ('error', 'm')}, send=boom, now=T0 + timedelta(hours=1))
    assert [e.kind for e in ev] == ['new'] and ok is False           # 例外同樣視為未送達、再試
    ev, ok = st.process({'k': ('error', 'm')}, send=OK, now=T0 + timedelta(hours=2))
    assert [e.kind for e in ev] == ['new'] and ok is True            # 成功才 commit
    assert st.process({'k': ('error', 'm')}, send=OK, now=T0 + timedelta(days=1))[0] == []   # 之後才抑制


def test_separate_namespaces_do_not_cross_resolve(tmp_path):
    """review P1：兩個 producer 各自只知道自己的 key，不可互相判成已恢復。"""
    hc = AlertState(tmp_path / 'hc.json', namespace='healthcheck')
    sy = AlertState(tmp_path / 'sy.json', namespace='sync')
    hc.process({'JWT Token': ('error', 'x'), '上傳暫停': ('warning', 'y')}, send=OK, now=T0)
    # 同步成功：sync producer 傳空 current，不得把 health 的 key 當成 resolved
    ev, _ = sy.process({}, send=OK, now=T0 + timedelta(hours=2))
    assert ev == []
    # 隔天 health 仍存在同樣問題 → 抑制（去重未失效）
    ev, _ = hc.process({'JWT Token': ('error', 'x'), '上傳暫停': ('warning', 'y')}, send=OK, now=T0 + timedelta(days=1))
    assert ev == []
    # 反方向：health 這輪沒有「同步執行」key，不得把 sync 的失敗判成已恢復
    sy.process({'同步執行': ('error', 'disk full')}, send=OK, now=T0 + timedelta(days=1, hours=1))
    ev, _ = hc.process({'JWT Token': ('error', 'x')}, send=OK, now=T0 + timedelta(days=2))
    assert [e.key for e in ev if e.kind == 'resolved'] == ['上傳暫停']      # 只解除自己 namespace 的 key
    assert sy.load().get('同步執行') is not None                          # sync 的告警仍在


def test_default_namespace_paths_are_distinct():
    assert AlertState(namespace='healthcheck').path != AlertState(namespace='sync').path
