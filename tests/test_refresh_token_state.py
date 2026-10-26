"""refresh token 的狀態判讀：判讀失敗要有自己的狀態，不可以混進「快過期」。

PR #125 第一輪 review 的 P2：原本的判讀是寫在 run_weekly_sync.sh 裡的一段
inline python——

    payload = decode_jwt_payload(REFRESH_TOKEN)
    exp = payload.get('exp', 0)
    days_left = (exp - time.time()) / 86400     # exp 是字串就 TypeError

exp 不是數字時這一行拋 TypeError，python 以 **exit 1** 結束，而腳本把 1 解讀成
「快過期（<2 天）」→ 照樣放行三個 v2 步驟。

這正是我在 #124 犯過的同一種錯：`socket.gaierror` 是「我們想像中的 DNS 例外」，
而 `exit 1` 是「我們想像中的 warning」——真正的失敗模式沒被涵蓋。所以判讀搬到
可測試的模組，並給「讀不出來」一個**獨立的** exit code。
"""
import time

import pytest

from geobingan_sync.steps.check_refresh_token import classify, EXIT_CODES


def _token(payload_obj):
    import base64, json
    p = base64.urlsafe_b64encode(json.dumps(payload_obj).encode()).decode().rstrip('=')
    return 'eyJhbGciOiJIUzI1NiJ9.' + p + '.sig'


NOW = 1_800_000_000.0


class TestClassify:

    def test_a_healthy_token_is_ok(self):
        state, days, _ = classify(_token({'exp': NOW + 7 * 86400}), now=NOW)
        assert state == 'ok'
        assert days == pytest.approx(7.0, abs=0.01)

    def test_under_two_days_is_warning(self):
        state, days, _ = classify(_token({'exp': NOW + 86400}), now=NOW)
        assert state == 'warning'

    def test_a_past_exp_is_expired(self):
        state, days, _ = classify(_token({'exp': NOW - 86400}), now=NOW)
        assert state == 'expired'
        assert days == pytest.approx(-1.0, abs=0.01)

    @pytest.mark.parametrize('exp', ['not-a-number', '', None, [], {}, True, False])
    def test_a_non_numeric_exp_is_unreadable_not_warning(self, exp):
        """review 的重現案例：exp='not-a-number'。

        舊寫法在這裡拋 TypeError → exit 1 → 被當成 warning → 放行 v2。
        """
        state, days, detail = classify(_token({'exp': exp}), now=NOW)
        assert state == 'unreadable', f'exp={exp!r} 被判成 {state}'
        assert days is None
        assert detail, '要說得出為什麼讀不出來'

    def test_a_missing_exp_is_unreadable_not_expired(self):
        """沒有 exp ≠ 已過期。舊寫法用 `.get('exp', 0)` 當成 1970 年、報「已過期」。"""
        state, _, _ = classify(_token({'type': 'refresh'}), now=NOW)
        assert state == 'unreadable'

    @pytest.mark.parametrize('tok', ['', 'garbage', 'a.b', 'a.b.c.d', 'eyJhbGciOiJIUzI1NiJ9..sig'])
    def test_an_undecodable_token_is_unreadable(self, tok):
        state, _, _ = classify(tok, now=NOW)
        assert state == 'unreadable'

    def test_exactly_at_expiry_is_expired_not_warning(self):
        state, _, _ = classify(_token({'exp': NOW}), now=NOW)
        assert state == 'expired'


class TestExitCodes:
    """exit code 是腳本唯一讀得到的東西，所以它本身就是介面。"""

    def test_every_state_has_its_own_code(self):
        assert sorted(EXIT_CODES.values()) == [0, 1, 2, 3], EXIT_CODES

    def test_unreadable_is_not_the_same_code_as_warning(self):
        assert EXIT_CODES['unreadable'] != EXIT_CODES['warning'], \
            '判讀失敗和「快過期」用同一個 code，就會再被當成 warning 放行'

    def test_the_codes_match_what_the_shell_expects(self):
        assert EXIT_CODES['ok'] == 0
        assert EXIT_CODES['warning'] == 1
        assert EXIT_CODES['expired'] == 2
        assert EXIT_CODES['unreadable'] == 3

    def test_the_cli_exits_with_the_classified_code(self, tmp_path):
        import subprocess, sys, os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, REFRESH_TOKEN=_token({'exp': 'not-a-number'}))
        p = subprocess.run([sys.executable, '-m', 'geobingan_sync.steps.check_refresh_token'],
                           cwd=root, env=env, capture_output=True, text=True, timeout=60)
        assert p.returncode == 3, (p.returncode, p.stdout, p.stderr)
        assert 'UNREADABLE' in p.stdout
