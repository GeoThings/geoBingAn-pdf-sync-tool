"""步驟 1 不可以被 v2（riskmap.today）的狀態擋住。

背景（ClickUp z956jf7zrd，slayer 2026-10-25）：geoBingAn_v2_backend
（riskmap.today API 與 riskmap.tw 前端）要關閉，日期未定。

原本 `run_weekly_sync.sh` 在 refresh token **過期**時會 `exit 1`，而那一段在
**步驟 1 之前**。步驟 1 是台北市網站 → Google 雲端硬碟，用的是 Google 服務帳號
憑證，完全不需要 riskmap 的 token；而新版 riskmap.taipei 的資料源就是那個雲端
硬碟。所以舊寫法等於「讓一個步驟 1 用不到的憑證，擋掉唯一不能停的那一步」。

這有硬期限：現在那張 refresh token 2026-10-30 18:03 到期，而關站後就拿不到新的
——所以 11/02（一）那輪會在步驟 1 之前就 exit 1。

本檔 source 正式的 lib/v2_gate.sh 跑真邏輯，另外用原始碼斷言釘住
run_weekly_sync.sh 不准把這段邏輯自己再寫一份。
"""
import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, 'tests', 'fixtures', 'v2_gate_harness.sh')
SCRIPT = os.path.join(ROOT, 'run_weekly_sync.sh')


def _run(**env):
    e = dict(os.environ)
    e.update({k: str(v) for k, v in env.items()})
    p = subprocess.run(['bash', HARNESS], env=e, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    out = dict(
        line.split('=', 1) for line in p.stdout.strip().splitlines() if '=' in line
    )
    return out


class TestTokenStateNaming:
    """exit code 要先翻成有名字的狀態，呼叫端不要散落 magic number。"""

    @pytest.mark.parametrize('code,expected', [
        (0, 'ok'), (1, 'warning'), (2, 'expired'), (3, 'unknown'), (127, 'unknown'),
    ])
    def test_exit_code_maps_to_a_named_state(self, code, expected):
        assert _run(TOKEN_EXIT=code)['TOKEN_STATE'] == expected


class TestV2Usability:

    def test_a_valid_token_lets_the_v2_steps_run(self):
        out = _run(TOKEN_EXIT=0)
        assert out['V2_USABLE'] == '1'

    def test_a_nearly_expired_token_still_lets_them_run(self):
        """剩不到兩天只是提醒，不是停工理由。"""
        out = _run(TOKEN_EXIT=1)
        assert out['V2_USABLE'] == '1'

    def test_an_expired_token_skips_the_v2_steps_and_says_why(self):
        out = _run(TOKEN_EXIT=2)
        assert out['V2_USABLE'] == '0'
        assert 'riskmap' in out['REASON']
        assert 'z956jf7zrd' in out['REASON'], '理由要指到那張卡，不然三個月後沒人知道為什麼跳過'

    def test_an_unreadable_token_check_skips_the_v2_steps(self):
        """判讀不出來就當不能用（對 v2 fail-closed），但步驟 1 照跑。"""
        out = _run(TOKEN_EXIT=99)
        assert out['V2_USABLE'] == '0'
        assert out['REASON']


class TestFinalStatus:
    """狀態要能分辨三件不同的事，不可以只有 success／failure 兩格。"""

    def test_everything_fine_is_success(self):
        assert _run(TOKEN_EXIT=0, HAS_ERROR=0)['FINAL'] == 'success'

    def test_an_error_is_failure(self):
        assert _run(TOKEN_EXIT=0, HAS_ERROR=1)['FINAL'] == 'failure'

    def test_step1_ok_but_v2_skipped_is_partial_not_success(self):
        """不能記 success——那會把「追蹤報告已經停止更新」藏起來。"""
        assert _run(TOKEN_EXIT=2, HAS_ERROR=0)['FINAL'] == 'partial'

    def test_step1_ok_but_v2_skipped_is_partial_not_failure(self):
        """也不能記 failure——步驟 1 成功了，而那是現在最重要的一步。"""
        out = _run(TOKEN_EXIT=2, HAS_ERROR=0)
        assert out['FINAL'] != 'failure'

    def test_an_error_outranks_a_v2_skip(self):
        assert _run(TOKEN_EXIT=2, HAS_ERROR=1)['FINAL'] == 'failure'


class TestTheScriptUsesTheOneRule:
    """原始碼斷言：run_weekly_sync.sh 不准自己再寫一份判斷。"""

    @pytest.fixture(scope='class')
    def src(self):
        with open(SCRIPT, encoding='utf-8') as f:
            return f.read()

    def test_the_script_sources_the_shared_lib(self, src):
        assert re.search(r'^\s*\.\s+"?\$\{?SCRIPT_DIR\}?"?/lib/v2_gate\.sh', src, re.M), \
            'run_weekly_sync.sh 沒有 source lib/v2_gate.sh'

    def test_the_script_goes_through_the_shared_helpers(self, src):
        for fn in ('v2_token_state', 'v2_usable', 'sync_final_status'):
            assert fn in src, f'run_weekly_sync.sh 沒有用 {fn}'

    def test_an_expired_token_no_longer_exits_before_step_one(self, src):
        """這是整個修正的重點：過期不可以再讓腳本在步驟 1 之前結束。"""
        m = re.search(r'if \[ \$TOKEN_EXIT -eq 2 \]; then(.*?)\n(elif|fi)\b', src, re.S)
        assert m, '找不到 token 過期那段'
        block = m.group(1)
        assert not re.search(r'^\s*exit\s+\d', block, re.M), \
            'token 過期那段還有 exit——步驟 1 會被擋掉'

    def test_all_three_v2_dependent_steps_are_gated(self, src):
        """上傳、名稱比對、追蹤報告三步都要被同一個旗標守住。

        只斷言「旗標出現過」太鬆——拿掉其中一道閘門也會過（第一版就是這樣，
        注入測試抓到的）。這裡數閘門的數目。
        """
        gates = re.findall(r'\[\s*\$V2_SKIPPED\s+-ne\s+0\s*\]', src)
        assert len(gates) >= 3, (
            f'只找到 {len(gates)} 道 V2_SKIPPED 閘門，應該有三道'
            '（步驟 2 上傳、步驟 2.5 名稱比對、步驟 3 追蹤報告）')
        for marker in ('步驟 2/4', '步驟 2.5', '步驟 3'):
            assert marker in src

    def test_the_final_status_is_computed_from_the_shared_helper(self, src):
        assert re.search(r'sync_final_status\s+"\$HAS_ERROR"\s+"\$\{V2_SKIPPED', src), \
            'cleanup 沒有把 V2_SKIPPED 交給 sync_final_status'


# ----------------------------------------- partial 要看得見、而且說得出原因

class TestPartialKeepsItsReason:
    """partial 的原因原本會被丟掉——10/19 那輪的說明是人工補寫的。

    v2 關站後每輪都會是 partial，原因若不落檔，健檢就只能顯示「partial」
    卻講不出為什麼。
    """

    def test_a_failure_keeps_its_reason(self):
        from geobingan_sync.steps.record_sync_result import error_message_for
        assert error_message_for('failure', '步驟3: 生成報告失敗') == '步驟3: 生成報告失敗'

    def test_a_partial_keeps_its_reason(self):
        from geobingan_sync.steps.record_sync_result import error_message_for
        assert error_message_for('partial', 'token 已過期') == 'token 已過期'

    def test_a_success_keeps_none(self):
        """成功的輪次不要留一條假的錯誤訊息。"""
        from geobingan_sync.steps.record_sync_result import error_message_for
        assert error_message_for('success', '') is None
        assert error_message_for('success', '不該被留下') is None


class TestPartialIsVisibleButDoesNotShout:
    """看得見，但不要每 7 天 @人＋寄信——那是「永遠亮的告警」的另一種樣子。"""

    def test_a_partial_round_alerts_at_warning_level(self):
        from geobingan_sync.steps.record_sync_result import notify_sync_outcome
        from geobingan_sync.alert_state import AlertState
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            st = AlertState(pathlib.Path(d) / 'a.json', namespace='sync')
            events = notify_sync_outcome('partial', 'riskmap token 已過期',
                                         alert_state=st, send=lambda *a, **k: True)
        assert events, '沒有發出任何事件——那就看不見了'
        assert all(e.level == 'warning' for e in events), [e.level for e in events]

    def test_a_real_failure_still_alerts_at_error_level(self):
        from geobingan_sync.steps.record_sync_result import notify_sync_outcome
        from geobingan_sync.alert_state import AlertState
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            st = AlertState(pathlib.Path(d) / 'a.json', namespace='sync')
            events = notify_sync_outcome('failure', '步驟3: 生成報告失敗',
                                         alert_state=st, send=lambda *a, **k: True)
        assert events and all(e.level == 'error' for e in events)

    def test_a_success_round_raises_nothing(self):
        from geobingan_sync.steps.record_sync_result import notify_sync_outcome
        from geobingan_sync.alert_state import AlertState
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            st = AlertState(pathlib.Path(d) / 'a.json', namespace='sync')
            events = notify_sync_outcome('success', '', alert_state=st,
                                         send=lambda *a, **k: True)
        assert [e for e in events if e.kind != 'resolved'] == []


class TestHealthCheckSeesPartial:
    """健檢不可以把 partial 當成 ok。"""

    def _status_file(self, tmp_path, last_status, error=None):
        import json
        p = tmp_path / 'sync_status.json'
        hist = [{'date': '2026-11-02', 'time': '10:17:00', 'status': last_status,
                 'synced': 12, 'uploaded': 0, 'failed': 0, 'duration_seconds': 1000}]
        if error:
            hist[0]['error'] = error
        p.write_text(json.dumps({
            'last_run': '2026-11-02T10:17:00', 'last_status': last_status,
            'stats': {'total_runs': 1, 'success_count': 0, 'failure_count': 1},
            'history': hist,
        }), encoding='utf-8')
        return str(p)

    def test_partial_is_a_warning_not_ok(self, tmp_path):
        import health_check
        from datetime import datetime
        level, msg = health_check.check_last_sync(
            path=self._status_file(tmp_path, 'partial', 'riskmap token 已過期'),
            now=datetime(2026, 11, 2, 11, 0))
        assert level == 'warning', f'partial 被當成 {level}，報告停止更新就看不見了'
        assert 'riskmap token 已過期' in msg, '要說得出為什麼'

    def test_partial_without_a_reason_still_warns(self, tmp_path):
        import health_check
        from datetime import datetime
        level, msg = health_check.check_last_sync(
            path=self._status_file(tmp_path, 'partial'),
            now=datetime(2026, 11, 2, 11, 0))
        assert level == 'warning'
        assert '原因未記錄' in msg

    def test_success_is_still_ok(self, tmp_path):
        import health_check
        from datetime import datetime
        level, _ = health_check.check_last_sync(
            path=self._status_file(tmp_path, 'success'),
            now=datetime(2026, 11, 2, 11, 0))
        assert level == 'ok'
