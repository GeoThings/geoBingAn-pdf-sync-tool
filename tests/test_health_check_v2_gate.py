"""v2（riskmap.today）不可用時，健檢要「不查」，不是每天報一次查不到。

2026-10-31 到 11-09 連續 10 天，健檢每天印一次
`解析積壓檢查失敗: 401 Client Error: Unauthorized`——因為 refresh token 在
10/30 18:03 到期，換不到 JWT。那既不是「積壓正常」也不是「積壓出事」，是
**量不到**。同一段時間真正該被看到的新訊號（未支援來源 42 案、其中 4 案近
14 天新增）排在三盞永久亮著的燈後面——這就是永久紅燈的代價。

這一檔釘住三件事：
  1. 量不到就回 `SKIP`，而且**在發請求之前**就回。
  2. 「不再檢查」不可以被寫成「已恢復」——後者是斷言問題解決了。
  3. 「網路就緒」只由**告警通道**決定；被檢查的對象解不到，不等於告警送不出去。

最後三個是**刻意不閘門**的反例。閘門開太大就會被它們抓到——尤其
`check_token`：它是閘門自己的輸入，把它關掉等於把唯一還能動手的那盞燈關掉。
"""
import base64
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import health_check
from geobingan_sync.alert_state import AlertState, format_events, plan_alerts

T0 = datetime(2026, 11, 9, 8, 0)
REASON = 'riskmap token expired（已過期 9.6 天）；riskmap.today 即將關閉，見 ClickUp z956jf7zrd'


def _state(tmp_path):
    return AlertState(path=tmp_path / 'alert_state.json', namespace='healthcheck')


class _Send:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, title, body, needs_attention):
        self.calls.append((title, body, needs_attention))
        return self.ok


def _token(payload_obj):
    p = base64.urlsafe_b64encode(json.dumps(payload_obj).encode()).decode().rstrip('=')
    return 'eyJhbGciOiJIUzI1NiJ9.' + p + '.sig'


def _explode(*a, **kw):
    raise AssertionError('v2 不可用時不應該發出任何請求')


# ---------- 1. 量不到就不查 ----------

class TestTheBacklogCheckStopsAskingWhenItCannotMeasure:

    def test_it_returns_skip_and_sends_no_request(self, monkeypatch):
        import requests
        monkeypatch.setattr(requests, 'get', _explode)
        monkeypatch.setattr(health_check, '_api_token', _explode)
        level, msg = health_check.check_parse_backlog(now=T0, reason=REASON)
        assert level == health_check.SKIP, 'warning 會變成永遠亮著的燈；ok 是假綠燈'
        assert REASON in msg and '未取得 ≠ 0' in msg

    def test_skip_is_neither_ok_nor_a_problem(self):
        assert health_check.SKIP not in ('ok', 'warning', 'error')

    def test_it_still_measures_when_v2_is_usable(self, monkeypatch):
        """閘門只擋「不可用」那一面，健康的那一面必須照舊量。"""
        calls = []

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {'results': [], 'next': None}

        def fake_get(url, **kw):
            calls.append(url)
            return _Resp()

        import requests
        monkeypatch.setattr(requests, 'get', fake_get)
        monkeypatch.setattr(health_check, '_api_token', lambda: 'tok')
        level, msg = health_check.check_parse_backlog(now=T0, reason=None)
        assert level == 'ok' and calls, 'v2 可用時要真的去問'

    def test_the_rule_comes_from_v2_guard_not_a_local_copy(self, monkeypatch):
        """判準向 `v2_guard` 要。自己抄一份，兩邊就會在不同時間點改變主意（#126 的教訓）。"""
        import geobingan_sync.v2_guard as guard
        monkeypatch.setattr(guard, 'v2_unusable_reason', lambda *a, **k: '問到 v2_guard 了')
        import requests
        monkeypatch.setattr(requests, 'get', _explode)
        monkeypatch.setattr(health_check, '_api_token', _explode)
        level, msg = health_check.check_parse_backlog(now=T0)
        assert level == health_check.SKIP and '問到 v2_guard 了' in msg

    def test_a_usable_v2_is_injectable_so_the_default_cannot_hide_a_bug(self, monkeypatch):
        """`reason=None` 要代表「v2 可用」，不可以被當成「請去算」。"""
        import geobingan_sync.v2_guard as guard
        monkeypatch.setattr(guard, 'v2_unusable_reason',
                            lambda *a, **k: pytest.fail('傳了 None 就不該再去算'))
        monkeypatch.setattr(health_check, '_api_token', lambda: None)
        level, msg = health_check.check_parse_backlog(now=T0, reason=None)
        assert level == 'warning' and '無可用 JWT' in msg


# ---------- 2. 「不再檢查」不是「已恢復」 ----------

class TestAStoppedCheckIsNotARecovery:

    def test_a_skipped_check_is_not_an_issue_and_is_not_notified(self, tmp_path):
        send = _Send()
        issues, events = health_check.run_health_check(
            checks=[('解析積壓', lambda: (health_check.SKIP, REASON))],
            notify=True, alert_state=_state(tmp_path), now=T0, send=send, network_wait=False)
        assert issues == [] and events == []
        assert send.calls == [], '刻意沒查不該發通知'

    def test_it_is_still_visible_in_the_report(self, tmp_path, capsys):
        health_check.run_health_check(
            checks=[('解析積壓', lambda: (health_check.SKIP, REASON))],
            notify=False, alert_state=_state(tmp_path), now=T0)
        out = capsys.readouterr().out
        assert '⏭️' in out and REASON in out, '不算問題 ≠ 藏起來'
        assert '1 項刻意沒有檢查' in out

    def test_a_light_that_was_on_is_closed_as_not_checked(self, tmp_path):
        st = _state(tmp_path)
        st.process({'解析積壓': ('warning', '401 Unauthorized')}, send=_Send(), now=T0)
        send = _Send()
        health_check.run_health_check(
            checks=[('解析積壓', lambda: (health_check.SKIP, REASON))],
            notify=True, alert_state=st, now=T0 + timedelta(days=1), send=send,
            network_wait=False)
        assert len(send.calls) == 1
        title, body, _ = send.calls[0]
        assert '不再檢查：解析積壓' in body and REASON in body
        assert '已恢復' not in body, '它不是好了，是我們不再問了'
        assert '不再檢查' in title and '已恢復' not in title

    def test_a_real_recovery_still_says_recovered(self, tmp_path):
        """反方向的對照：真的好了就要說「已恢復」，不可以被新寫法蓋掉。"""
        st = _state(tmp_path)
        st.process({'解析積壓': ('warning', '401 Unauthorized')}, send=_Send(), now=T0)
        send = _Send()
        health_check.run_health_check(
            checks=[('解析積壓', lambda: ('ok', '近期解析佇列正常'))],
            notify=True, alert_state=st, now=T0 + timedelta(days=1), send=send,
            network_wait=False)
        _title, body, _ = send.calls[0]
        assert '已恢復：解析積壓' in body and '不再檢查' not in body

    def test_the_closing_notice_is_sent_once(self, tmp_path):
        st = _state(tmp_path)
        st.process({'解析積壓': ('warning', '401')}, send=_Send(), now=T0)
        send = _Send()
        for day in (1, 2, 3):
            health_check.run_health_check(
                checks=[('解析積壓', lambda: (health_check.SKIP, REASON))],
                notify=True, alert_state=st, now=T0 + timedelta(days=day), send=send,
                network_wait=False)
        assert len(send.calls) == 1, '關燈只需要講一次'

    def test_plan_alerts_keeps_recovery_and_stop_apart(self):
        prev = {'甲': {'level': 'warning', 'message': 'x', 'first_seen': T0.isoformat(),
                       'last_sent': T0.isoformat(), 'last_seen': T0.isoformat()},
                '乙': {'level': 'error', 'message': 'y', 'first_seen': T0.isoformat(),
                       'last_sent': T0.isoformat(), 'last_seen': T0.isoformat()}}
        events, _ = plan_alerts(prev, {}, T0, skipped={'乙': '量不到'})
        by_key = {e.key: e for e in events}
        assert by_key['甲'].note == '' and by_key['乙'].note == '量不到'
        _title, body, needs_attention = format_events(events, T0)
        assert '已恢復：甲' in body and '不再檢查：乙（量不到）' in body
        assert needs_attention is True, 'error 那一盞被關掉要讓人知道一次'


# ---------- 3. 網路就緒只看告警通道 ----------

class TestReadinessIsAboutTheAlertChannelsOnly:

    def test_a_dead_measured_host_does_not_make_the_network_unready(self, monkeypatch, capsys):
        """關站後 riskmap.today 永遠解不到，但告警一直送得出去——不可以每天報一次。"""
        monkeypatch.setattr(health_check, 'alert_channel_hosts', lambda: ['api.clickup.com'])
        monkeypatch.setattr(health_check, 'measured_hosts', lambda: ['riskmap.today'])

        # 逐 host 判斷，不是「清單裡有 clickup 就算就緒」——後者在「兩群合成一份
        # 清單去等」的回歸下照樣會過（2026-11-09 注入⑥實測漏掉，就是這個毛病）。
        dead = {'riskmap.today'}

        def fake_wait(hosts, timeout=0):
            unresolved = [h for h in hosts if h in dead]
            return (not unresolved), unresolved

        ready, unresolved = health_check.wait_for_alert_network(wait=fake_wait)
        out = capsys.readouterr().out
        assert ready is True, '告警送得出去就算就緒'
        assert unresolved == ['riskmap.today'], '解不到還是要講，只是不決定 ready'
        assert '檢查與告警可能失敗' not in out, '舊版那句話在這個情境下是假的'
        assert '不影響告警送達' in out

    def test_an_unreachable_channel_still_fails_readiness(self, monkeypatch):
        monkeypatch.setattr(health_check, 'alert_channel_hosts', lambda: ['api.clickup.com'])
        monkeypatch.setattr(health_check, 'measured_hosts', lambda: [])
        ready, unresolved = health_check.wait_for_alert_network(
            wait=lambda hosts, timeout=0: (False, list(hosts)))
        assert ready is False and unresolved == ['api.clickup.com']

    def test_the_measured_host_gets_a_shorter_budget(self, monkeypatch):
        monkeypatch.setattr(health_check, 'alert_channel_hosts', lambda: ['api.clickup.com'])
        monkeypatch.setattr(health_check, 'measured_hosts', lambda: ['riskmap.today'])
        seen = []

        def fake_wait(hosts, timeout=0):
            seen.append((tuple(hosts), timeout))
            return True, []

        health_check.wait_for_alert_network(timeout=90.0, wait=fake_wait)
        budgets = dict((h[0], t) for h, t in seen)
        assert budgets['api.clickup.com'] == 90.0
        assert budgets['riskmap.today'] <= health_check.MEASURED_DNS_TIMEOUT < 90.0

    def test_both_groups_are_still_waited_for(self, monkeypatch):
        monkeypatch.setattr(health_check, 'alert_channel_hosts', lambda: ['api.clickup.com'])
        monkeypatch.setattr(health_check, 'measured_hosts', lambda: ['riskmap.today'])
        seen = []

        def fake_wait(hosts, timeout=0):
            seen.append(tuple(hosts))
            return True, []

        health_check.wait_for_alert_network(wait=fake_wait)
        assert seen == [('api.clickup.com',), ('riskmap.today',)], \
            '被檢查對象也要等（避開 post-wake DNS race），只是不決定 ready'

    def test_an_explicit_host_list_keeps_the_old_meaning(self):
        ready, unresolved = health_check.wait_for_alert_network(
            timeout=1.0, hosts=['nope.invalid'],
            wait=lambda hosts, timeout=0: (False, list(hosts)))
        assert ready is False and unresolved == ['nope.invalid']

    def test_alert_hosts_still_lists_both_groups(self, monkeypatch):
        monkeypatch.setattr(health_check, 'alert_channel_hosts', lambda: ['api.clickup.com'])
        monkeypatch.setattr(health_check, 'measured_hosts', lambda: ['riskmap.today'])
        assert health_check.alert_hosts() == ['riskmap.today', 'api.clickup.com']

    def test_nothing_to_wait_for_is_ready(self, monkeypatch):
        monkeypatch.setattr(health_check, 'alert_channel_hosts', lambda: [])
        monkeypatch.setattr(health_check, 'measured_hosts', lambda: [])
        assert health_check.wait_for_alert_network(wait=_explode) == (True, [])


# ---------- 4. 刻意不閘門的三個反例 ----------

class TestTheChecksThatMustNeverBeGated:
    """閘門開太大的反例。每一項都有「為什麼不能關」的理由，不是漏掉。"""

    def test_the_token_check_is_never_gated_by_the_token_being_dead(self, monkeypatch):
        """閘門自己的輸入就是 token 狀態。把這盞燈關掉，就沒有人會去換 token 了。"""
        import geobingan_sync.v2_guard as guard
        monkeypatch.setattr(guard, 'v2_unusable_reason', lambda *a, **k: REASON)
        monkeypatch.setenv('REFRESH_TOKEN', _token({'exp': 1_700_000_000}))
        level, msg = health_check.check_token()
        assert level == 'error' and health_check.SKIP != level
        assert '重新登入' in msg, '唯一還能動手的那盞燈要留著，而且要說怎麼動手'

    def test_the_api_reachability_check_is_never_gated(self, monkeypatch):
        """2026-11-09 的教訓：它是唯一能分辨「關站了」和「只是 token 過期」的訊號。

        那天 token 已經死了 9.6 天，而 `riskmap.tw`／`riskmap.today` 都還回 200
        ——報告停更 10 天的真正原因是沒換 token，不是關站。要是這一項也被同一道
        閘門關掉，就沒有任何地方會告訴我們這件事。
        """
        import geobingan_sync.v2_guard as guard
        monkeypatch.setattr(guard, 'v2_unusable_reason', lambda *a, **k: REASON)
        calls = []

        class _Resp:
            status_code = 200

        import requests
        monkeypatch.setattr(requests, 'get', lambda url, **kw: (calls.append(url), _Resp())[1])
        level, msg = health_check.check_api()
        assert level == 'ok' and calls, '不需要憑證的可達性檢查要照跑'

    def test_the_local_checks_are_never_gated(self, monkeypatch):
        """`check_pause`／`check_budget` 量的是本機的檔，和 v2 可不可用無關。"""
        import geobingan_sync.v2_guard as guard
        import time as _time
        monkeypatch.setattr(guard, 'v2_unusable_reason', lambda *a, **k: REASON)
        monkeypatch.setattr(health_check.os.path, 'exists', lambda p: True)
        monkeypatch.setattr(health_check.os.path, 'getmtime', lambda p: _time.time() - 38 * 86400)
        level, msg = health_check.check_pause()
        assert level == 'error' and '--catchup-days' in msg
