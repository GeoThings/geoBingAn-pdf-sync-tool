"""upload_to_geobingan 回應分類（預算結算）：4xx→False（確定零成本）；5xx→None（結果不明，保守計入）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import geobingan_sync.steps.upload_pdfs as up


class _R:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._p = payload or {}
        self.text = str(self._p)

    def json(self):
        return self._p


def _setup(monkeypatch, responses, refresh=('newtok', 'newref')):
    """responses：依序回給每次 requests.post 的 _R。"""
    seq = list(responses)
    calls = []
    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        calls.append(headers.get('Authorization'))
        return seq.pop(0)
    monkeypatch.setattr(up.requests, 'post', fake_post)
    monkeypatch.setattr(up, '_get_valid_token', lambda: 'tok')
    monkeypatch.setattr(up.time, 'sleep', lambda s: None)
    monkeypatch.setattr(up, 'refresh_access_token', lambda r, u: refresh)
    monkeypatch.setattr(up, 'update_config_token', lambda t, r=None: None)
    return calls


def test_500_is_unknown_not_refunded(monkeypatch):
    """review P1：500 可能發生在後端已建報告/進佇列之後 → None（保守計入），不可視為零成本。"""
    _setup(monkeypatch, [_R(500)])
    assert up.upload_to_geobingan(b'%PDF', 'a.pdf', 'X') is None


def test_400_is_definite_rejection(monkeypatch):
    _setup(monkeypatch, [_R(400, {'detail': 'bad'})])
    assert up.upload_to_geobingan(b'%PDF', 'a.pdf', 'X') is False


def test_503_retries_exhausted_is_unknown(monkeypatch):
    calls = _setup(monkeypatch, [_R(503), _R(503), _R(503)])
    assert up.upload_to_geobingan(b'%PDF', 'a.pdf', 'X', max_retries=3) is None
    assert len(calls) == 3


def test_401_refresh_then_retry_500_is_unknown(monkeypatch):
    """review P1：401 換發後重試得到 5xx → None，不可一律 False。"""
    calls = _setup(monkeypatch, [_R(401), _R(500)])
    assert up.upload_to_geobingan(b'%PDF', 'a.pdf', 'X') is None
    assert calls[-1] == 'Bearer newtok'


def test_401_refresh_then_retry_400_is_rejection(monkeypatch):
    _setup(monkeypatch, [_R(401), _R(400)])
    assert up.upload_to_geobingan(b'%PDF', 'a.pdf', 'X') is False


def test_401_refresh_failed_is_rejection_no_second_post(monkeypatch):
    calls = _setup(monkeypatch, [_R(401)], refresh=(None, None))
    assert up.upload_to_geobingan(b'%PDF', 'a.pdf', 'X') is False
    assert len(calls) == 1


def test_2xx_and_502_count_as_delivered(monkeypatch):
    _setup(monkeypatch, [_R(201, {'id': 'r1'})])
    assert up.upload_to_geobingan(b'%PDF', 'a.pdf', 'X')['id'] == 'r1'
    _setup(monkeypatch, [_R(502)])
    assert up.upload_to_geobingan(b'%PDF', 'a.pdf', 'X')['status'] == 'processing'
