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
        state, days, detail = classify(tok, now=NOW)
        assert state == 'unreadable'
        assert days is None, f'讀不出來卻給了 {days} 這個數字'
        assert detail

    @pytest.mark.parametrize('tok', [
        '', 'garbage', 'a.b', 'eyJhbGciOiJIUzI1NiJ9..sig',
        _token({'type': 'refresh'}),
        _token({'exp': 'not-a-number'}),
        _token({'exp': None}),
        _token({'exp': True}),
    ])
    def test_unreadable_never_carries_a_number(self, tok):
        """沒量到就不要給數字——給了就會被當成真的。

        第一版只在「exp 不是數字」那條路斷言 days is None，「payload 解不出來」
        那條沒斷言，於是注入「編一個 99.0 出來」沒被抓到。這一條把所有讀不出來
        的輸入都蓋住。
        """
        state, days, _ = classify(tok, now=NOW)
        assert state == 'unreadable'
        assert days is None

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


class TestNumericButNotUsable:
    """review r2 的 P2：只驗型別不夠，數值本身也要是「能用的」。

    - `exp: 1e309` 經 json 解析是 `inf`（JSON 沒有 Infinity 字面值，但 1e309
      會溢位成 inf）。`isinstance(inf, float)` 是 True，於是判成「ok、剩 inf 天」
      ——一張永不過期的 token。
    - `exp: 10**400` 是任意精度整數，`float(exp)` 拋 `OverflowError`。原本沒有
      捕捉，例外一路往上、CLI 以 exit 1 結束，**又被 shell 當成 warning**。

    第二點是同一個失敗類別的第三次（前兩次：inline python 的 TypeError、
    #124 的 socket.gaierror）。所以除了補這兩個案例，main() 也要兜底：
    任何沒預料到的例外都回 unreadable 的 code，永遠不會再變成 warning。
    """

    @pytest.mark.parametrize('exp,why', [
        (1e309, 'json 解析成 inf'),
        (float('inf'), 'inf'),
        (float('-inf'), '-inf'),
        (10 ** 400, '任意精度整數，float() 會溢位'),
        (-(10 ** 400), '負的任意精度整數'),
    ])
    def test_a_non_finite_or_overflowing_exp_is_unreadable(self, exp, why):
        import json
        try:
            json.dumps({'exp': exp})
        except ValueError:
            pytest.skip(f'json 無法序列化（{why}）')
        state, days, detail = classify(_token({'exp': exp}), now=NOW)
        assert state == 'unreadable', f'{why} 被判成 {state}（days={days}）'
        assert days is None
        assert detail

    def test_an_infinite_exp_does_not_become_a_token_that_never_expires(self):
        state, days, _ = classify(_token({'exp': 1e309}), now=NOW)
        assert state != 'ok', '無限大的 exp 變成「永不過期」'
        assert days != float('inf')

    def test_an_overflowing_exp_raises_nothing(self):
        """不可以讓例外逃出 classify——逃出去就會變成 exit 1＝warning。"""
        state, _, _ = classify(_token({'exp': 10 ** 400}), now=NOW)
        assert state == 'unreadable'


class TestMainNeverDegradesToWarning:
    """兜底：任何沒預料到的失敗都要回 unreadable 的 exit code，不能是 1。"""

    def test_an_unexpected_exception_exits_unreadable(self, monkeypatch):
        from geobingan_sync.steps import check_refresh_token as m
        monkeypatch.setattr(m, 'classify',
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
        rc = m.main()
        assert rc == EXIT_CODES['unreadable'], \
            f'沒預料到的例外回 {rc}；若是 1 就會被 shell 當成「快過期」放行 v2'

    def test_a_healthy_token_still_exits_zero(self, monkeypatch):
        from geobingan_sync.steps import check_refresh_token as m
        monkeypatch.setattr(m, 'classify', lambda *a, **k: ('ok', 7.0, '剩 7.0 天'))
        assert m.main() == 0
