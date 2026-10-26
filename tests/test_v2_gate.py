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
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, 'tests', 'fixtures', 'v2_gate_harness.sh')
SCRIPT = os.path.join(ROOT, 'run_weekly_sync.sh')


def code_only(text: str) -> str:
    """只留可執行的行。

    原始碼斷言必須分清楚程式與註解，否則兩個方向都會判錯：第一次是我寫在註解
    裡的「原本這段直接呼叫 send_notification(mention=True)」讓測試誤判紅；
    第二次是註解裡的「exit 4 = …刻意跳過」讓「跳過分支被拿掉」的注入沒被抓到。
    所以抽成共用的一個，所有 shell 原始碼斷言都先過它。
    """
    return '\n'.join(l for l in text.splitlines() if not l.lstrip().startswith('#'))


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


# ------------------------------------------- PR #125 第一輪 review 的三項

class TestNoStaleDataIsDressedUpAsThisWeek:
    """review P1：跳過追蹤報告卻讓快照與週報照跑，等於把舊資料包裝成本週結果。

    快照讀的 `permit_system_mapping.json` 與 `permit_registry.json` **只由步驟
    2.5 與步驟 3 更新**，而 v2 不可用時那兩步都被跳過。照寫下去會產生一份日期是
    今天、內容卻沒量到的快照；趨勢是前後兩份快照相減，於是輸出「建案 +0、AI +0」
    ——而「+0」和「沒量到」在那個輸出裡分不出來。未取得 ≠ 0。

    週報更嚴重：它會**自動上傳 ClickUp**，收到的人看不出資料其實停在關站前。
    """

    @pytest.fixture(scope='class')
    def src(self):
        with open(SCRIPT, encoding='utf-8') as f:
            return f.read()

    def test_the_snapshot_is_gated(self, src):
        """快照必須在 V2_SKIPPED 閘門的 else 分支裡。

        第一版只檢查「weekly_snapshot 前面附近有 V2_SKIPPED」——那驗的是距離
        不是結構，把 else 換成 `fi` ＋ `if true` 照樣會過（注入測試抓到的）。
        這裡改成：跳過訊息與 weekly_snapshot 之間必須有 else、而且不能先出現
        一行 `fi` 把閘門關掉。
        """
        assert 'if [ $V2_SKIPPED -ne 0 ]; then' in src
        i = src.find('跳過快照')
        assert i > 0, '找不到跳過快照的訊息'
        j = src.find('geobingan_sync.steps.weekly_snapshot', i)
        assert j > i, '跳過訊息之後找不到 weekly_snapshot 的呼叫'
        between = src[i:j]
        assert re.search(r'^\s*else\s*$', between, re.M), \
            '快照不在閘門的 else 分支裡'
        assert not re.search(r'^\s*fi\b', between, re.M), \
            '閘門在 weekly_snapshot 之前就被關掉了——等於沒守住'

    def test_the_monday_weekly_report_is_gated(self, src):
        assert re.search(r'if \[ "\$WEEKDAY" = "1" \] && \[ \$V2_SKIPPED -ne 0 \]', src), \
            '步驟 5（週一週報，會自動上傳 ClickUp）沒有被 V2_SKIPPED 閘門守住'

    def test_five_gates_now(self, src):
        """步驟 2、2.5、3、快照、步驟 5。"""
        gates = re.findall(r'\[\s*\$V2_SKIPPED\s+-ne\s+0\s*\]', src)
        assert len(gates) >= 5, f'只找到 {len(gates)} 道閘門，應該有五道'


class TestTheTokenCheckIsTestableAndHasItsOwnFailureState:
    """review P2：inline python 的例外以 exit 1 結束，被當成「快過期」放行 v2。"""

    @pytest.fixture(scope='class')
    def src(self):
        with open(SCRIPT, encoding='utf-8') as f:
            return f.read()

    def test_the_script_calls_the_module(self, src):
        assert 'geobingan_sync.steps.check_refresh_token' in src, \
            '腳本沒有走可測試的判讀模組'

    def test_the_inline_decode_is_gone(self, src):
        assert 'decode_jwt_payload' not in src, \
            '腳本裡還有 inline 的 JWT 判讀——那一段測不到，問題就是躲在那裡'

    def test_the_script_handles_the_unreadable_code(self, src):
        assert re.search(r'\$TOKEN_EXIT -eq 3', src), \
            '腳本沒有處理「讀不出來」（exit 3）這個狀態'

    def test_an_unreadable_token_makes_v2_unusable(self):
        """exit 3 → unknown → v2 不可用，但步驟 1 照跑。"""
        out = _run(TOKEN_EXIT=3, HAS_ERROR=0)
        assert out['TOKEN_STATE'] == 'unknown'
        assert out['V2_USABLE'] == '0'
        assert out['FINAL'] == 'partial'


class TestTheExpiredBranchDoesNotBypassTheAlertStateMachine:
    """review P3：過期分支每輪直接 @，抵銷「warning、不 @、七天提醒」的政策。"""

    @pytest.fixture(scope='class')
    def src(self):
        with open(SCRIPT, encoding='utf-8') as f:
            return f.read()

    _code_only = staticmethod(code_only)

    def test_the_expired_branch_sends_no_direct_mention(self, src):
        m = re.search(r'if \[ \$TOKEN_EXIT -eq 2 \]; then(.*?)\nelif \[ \$TOKEN_EXIT', src, re.S)
        assert m, '找不到 token 過期那段'
        code = self._code_only(m.group(1))
        assert 'mention=True' not in code, \
            '過期分支還在直接 @——v2 關站後會變成每輪 @ 一次'
        assert 'send_notification' not in code, \
            '過期分支還在繞過告警狀態機直接發通知'

    def test_the_expired_branch_still_says_where_the_alert_goes(self, src):
        m = re.search(r'if \[ \$TOKEN_EXIT -eq 2 \]; then(.*?)\nelif \[ \$TOKEN_EXIT', src, re.S)
        block = m.group(1)
        assert 'partial' in block, '沒說清楚告警改走哪裡，下次有人會以為漏掉了'


# -------------------------------- review r2 的 P1：守門要放在產物的邊界

class TestTheGuardIsAtTheArtifactBoundary:
    """閘門放在呼叫點就得靠呼叫點自律，而呼叫點會長出新的。

    我第一版只加在 run_weekly_sync.sh，漏了 run_friday_report.sh——那支週五
    17:00 的 job 自己呼叫 `generate_weekly_report --type summary --upload`，
    照樣用舊 mapping 產週報並自動上傳 ClickUp。
    """

    def test_an_unusable_v2_yields_a_reason(self):
        from geobingan_sync.v2_guard import v2_unusable_reason
        import base64, json
        p = base64.urlsafe_b64encode(json.dumps({'exp': 1}).encode()).decode().rstrip('=')
        reason = v2_unusable_reason(token='eyJhbGciOiJIUzI1NiJ9.' + p + '.sig')
        assert reason and 'z956jf7zrd' in reason

    def test_a_healthy_v2_yields_no_reason(self):
        from geobingan_sync.v2_guard import v2_unusable_reason
        import base64, json, time
        p = base64.urlsafe_b64encode(
            json.dumps({'exp': time.time() + 7 * 86400}).encode()).decode().rstrip('=')
        assert v2_unusable_reason(token='eyJhbGciOiJIUzI1NiJ9.' + p + '.sig') is None

    def test_a_nearly_expired_token_does_not_block_the_artifacts(self):
        """剩不到兩天只是提醒——這時資料還是新的，照產。"""
        from geobingan_sync.v2_guard import v2_unusable_reason
        import base64, json, time
        p = base64.urlsafe_b64encode(
            json.dumps({'exp': time.time() + 3600}).encode()).decode().rstrip('=')
        assert v2_unusable_reason(token='eyJhbGciOiJIUzI1NiJ9.' + p + '.sig') is None

    def test_refuse_returns_a_skip_code_distinct_from_failure(self):
        from geobingan_sync.v2_guard import refuse_if_v2_unusable, SKIP_EXIT_CODE
        import io as _io
        buf = _io.StringIO()
        rc = refuse_if_v2_unusable('週報', reason='token 過期', out=buf)
        assert rc == SKIP_EXIT_CODE
        assert SKIP_EXIT_CODE not in (0, 1), '刻意跳過不可以和成功或一般失敗共用 code'
        assert '跳過週報' in buf.getvalue()

    def test_refuse_lets_a_healthy_round_through(self):
        from geobingan_sync.v2_guard import refuse_if_v2_unusable
        import io as _io
        buf = _io.StringIO()
        assert refuse_if_v2_unusable('週報', reason=None, out=buf) is None
        assert buf.getvalue() == ''

    def test_the_weekly_report_cli_refuses_before_doing_any_work(self, monkeypatch):
        """邊界要擋在「做任何事之前」，不是產好了才不上傳。"""
        from geobingan_sync.steps import generate_weekly_report as g
        from geobingan_sync.v2_guard import SKIP_EXIT_CODE

        def _boom(*a, **k):
            raise AssertionError('v2 不可用時還是去讀資料／上傳了')

        monkeypatch.setattr(g, 'load_data', _boom)
        monkeypatch.setattr(g, 'upload_to_clickup', _boom)
        # 要 patch 守門自己模組裡的那一個——refuse_if_v2_unusable 內部呼叫的是
        # v2_guard.v2_unusable_reason，patch 到步驟模組上不會生效（第一版就錯在這）。
        monkeypatch.setattr('geobingan_sync.v2_guard.v2_unusable_reason',
                            lambda *a, **k: 'token 過期')
        monkeypatch.setattr(sys, 'argv', ['x', '--type', 'summary', '--upload'])
        assert g.main() == SKIP_EXIT_CODE

    def test_the_snapshot_cli_refuses_before_doing_any_work(self, monkeypatch):
        from geobingan_sync.steps import weekly_snapshot as w
        from geobingan_sync.v2_guard import SKIP_EXIT_CODE

        def _boom(*a, **k):
            raise AssertionError('v2 不可用時還是寫了快照')

        monkeypatch.setattr(w, 'save_snapshot', _boom)
        monkeypatch.setattr('geobingan_sync.v2_guard.v2_unusable_reason',
                            lambda *a, **k: 'token 過期')
        monkeypatch.setattr(sys, 'argv', ['x', '--notify'])
        assert w.main() == SKIP_EXIT_CODE


class TestEveryEntryPointHonoursTheSkip:
    """每個入口都要把「刻意跳過」和「失敗」分開寫，否則日誌會誤導。"""

    @pytest.fixture(scope='class')
    def friday(self):
        with open(os.path.join(ROOT, 'run_friday_report.sh'), encoding='utf-8') as f:
            return f.read()

    def test_the_friday_script_branches_on_the_skip_code(self, friday):
        """要有一道**真的分支**判斷跳過的 exit code，不是註解裡提到它。"""
        from geobingan_sync.v2_guard import SKIP_EXIT_CODE
        code = code_only(friday)
        assert re.search(rf'-eq\s+{SKIP_EXIT_CODE}\s*\]', code), \
            'run_friday_report.sh 沒有一道判斷「刻意跳過」exit code 的分支'
        assert '跳過' in code, '跳過時的訊息不在程式碼裡（只寫在註解不算）'

    def test_the_friday_script_reads_the_real_exit_code(self, friday):
        """`if cmd | tee` 讀到的是 tee 的 exit code，必須用 PIPESTATUS。"""
        code = code_only(friday)
        assert 'PIPESTATUS' in code, \
            '沒用 PIPESTATUS，讀到的會是 tee 的結束碼而不是週報程式的'

    def test_the_friday_script_does_not_claim_success_when_skipped(self, friday):
        from geobingan_sync.v2_guard import SKIP_EXIT_CODE
        code = code_only(friday)
        m = re.search(rf'-eq\s+{SKIP_EXIT_CODE}\s*\]', code)
        assert m, '找不到判斷跳過的分支'
        branch = code[m.start():].split('else')[0]
        assert '已產生並上傳' not in branch, '跳過時還是說「已產生並上傳」'
        assert '失敗' not in branch, '跳過時還是說「失敗」——會讓人去查錯的方向'
