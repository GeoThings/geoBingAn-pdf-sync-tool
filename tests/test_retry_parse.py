"""重推指令的查詢階段必須 fail-closed（PR #85 review P2）。

原本 `fetch_retryable()` 把所有例外一律 continue，於是網路中斷、後端 500、
401 過期、非 JSON 回應都被歸成「這份不需重推」；整批查詢都掛掉時會印出
「✅ 沒有需要重推的報告」並以 exit 0 結束，操作者以為積壓清空了，其實一份
都沒查到。這裡逐一釘住每種失敗形態，確保「不知道」不會被講成「沒有」。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.steps import retry_parse
from geobingan_sync.steps.retry_parse import fetch_retryable


class _Resp:
    """可注入的假回應；payload 為 Exception 時 .json() 直接 raise。"""
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeRequests:
    """script: {id: _Resp 或 Exception 實例（get 時 raise）}。"""
    def __init__(self, script):
        self.script = script
        self.calls = []

    def get(self, url, **kwargs):
        rid = url.rstrip('/').split('/')[-1]
        self.calls.append(rid)
        item = self.script[rid]
        if isinstance(item, Exception):
            raise item
        return item


def _patch(monkeypatch, script):
    fake = _FakeRequests(script)
    monkeypatch.setattr(retry_parse, 'requests', fake)
    return fake


# ---------- 各種失敗形態都要進 failures，不可被當成「不需重推」 ----------

@pytest.mark.parametrize('label,item,expect_reason', [
    ('網路中斷', OSError('connection reset'), 'OSError'),
    ('逾時', TimeoutError('read timed out'), 'TimeoutError'),
    ('後端 500', _Resp(500), 'HTTP 500'),
    ('token 過期 401', _Resp(401), 'HTTP 401'),
    ('無權限 403', _Resp(403), 'HTTP 403'),
    ('非 JSON 回應', _Resp(200, ValueError('Expecting value')), 'ValueError'),
    ('回應不是物件', _Resp(200, ['unexpected']), '回應格式異常'),
    ('缺 parse_status', _Resp(200, {'id': 'r1'}), '缺少 parse_status'),
])
def test_query_failures_are_never_silently_skipped(monkeypatch, label, item, expect_reason):
    _patch(monkeypatch, {'r1': item})
    targets, failures = fetch_retryable(['r1'], {})
    assert targets == [], f'{label}：不可被判定為「不需重推」'
    assert len(failures) == 1 and failures[0][0] == 'r1'
    assert expect_reason in failures[0][1], f'{label}：{failures[0][1]}'


def test_completed_report_is_genuinely_not_retryable(monkeypatch):
    """真的查到 completed，才算「不需重推」——這一項不進 failures。"""
    _patch(monkeypatch, {'r1': _Resp(200, {'parse_status': 'completed'})})
    targets, failures = fetch_retryable(['r1'], {})
    assert targets == [] and failures == []


def test_pending_and_failed_are_retryable(monkeypatch):
    _patch(monkeypatch, {'a': _Resp(200, {'parse_status': 'pending'}),
                         'b': _Resp(200, {'parse_status': 'failed'}),
                         'c': _Resp(200, {'parse_status': 'processing'})})
    targets, failures = fetch_retryable(['a', 'b', 'c'], {})
    assert targets == ['a', 'b'] and failures == []


def test_partial_failure_keeps_good_targets_and_reports_the_rest(monkeypatch):
    """一部分查得到、一部分查不到：兩邊都要如實回報，不可只回其中一邊。"""
    _patch(monkeypatch, {'a': _Resp(200, {'parse_status': 'pending'}),
                         'b': OSError('boom'),
                         'c': _Resp(200, {'parse_status': 'completed'})})
    targets, failures = fetch_retryable(['a', 'b', 'c'], {})
    assert targets == ['a']
    assert [f[0] for f in failures] == ['b']


# ---------- main()：有查詢失敗時絕不宣告「沒有需要重推」 ----------

def _run_main(monkeypatch, script, ids, capsys, tmp_path):
    _patch(monkeypatch, script)
    monkeypatch.setattr(retry_parse, '_get_valid_token', lambda: 'tok')
    code = retry_parse.main(ids, budget_path=tmp_path / 'budget.json')
    return code, capsys.readouterr().out


def test_all_queries_fail_does_not_claim_nothing_to_do(monkeypatch, capsys, tmp_path):
    """整批查詢失敗 → 非零 exit，且不得出現「沒有需要重推」。"""
    code, out = _run_main(monkeypatch,
                          {'a': OSError('down'), 'b': _Resp(500)},
                          ['a', 'b'], capsys, tmp_path)
    assert code == 4, code
    assert '沒有需要重推的報告' not in out
    assert '查詢失敗' in out and '狀態未知' in out


def test_genuinely_zero_targets_exits_clean(monkeypatch, capsys, tmp_path):
    """真的查清楚、確實沒有待重推 → exit 0 並明說沒有需要重推。"""
    code, out = _run_main(monkeypatch,
                          {'a': _Resp(200, {'parse_status': 'completed'}),
                           'b': _Resp(200, {'parse_status': 'completed'})},
                          ['a', 'b'], capsys, tmp_path)
    assert code == 0
    assert '沒有需要重推的報告' in out


def test_partial_failure_returns_nonzero_even_after_sending(monkeypatch, capsys, tmp_path):
    """送出了一部分，但有查詢失敗未納入 → 仍回非零，提醒排除後重跑。"""
    script = {'a': _Resp(200, {'parse_status': 'pending'}), 'b': OSError('down')}
    _patch(monkeypatch, script)
    monkeypatch.setattr(retry_parse, '_get_valid_token', lambda: 'tok')
    monkeypatch.setattr(retry_parse.time, 'sleep', lambda s: None)

    sent = []

    class _Post:
        status_code = 202

    def _post(url, **kwargs):
        sent.append(url)
        return _Post()

    script_requests = retry_parse.requests
    monkeypatch.setattr(script_requests, 'post', _post, raising=False)

    code = retry_parse.main(['a', 'b'], budget_path=tmp_path / 'budget.json')
    out = capsys.readouterr().out
    assert sent, '可重試的那份仍應送出'
    assert code == 4, '有查詢失敗就不能回 0'
    assert '未納入本批次' in out
