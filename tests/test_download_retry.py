"""Tests for PermitSync.download_pdf_list retry-with-backoff (#59).

retry 是錯誤處理路徑、production 最少被走到，這裡用注入的假 requests + 假 sleep
驗 retry loop 真的會重試（而非第一次就放棄）、滿 max_attempts 後才 sys.exit(1)。
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sync_permits
from sync_permits import PermitSync


class _FakeResp:
    def __init__(self, content=b'%PDF-1.4 fake'):
        self.content = content


class _FakeRequests:
    """script 為 list，元素是 _FakeResp（回傳）或 Exception 實例（raise）。"""
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _make_sync():
    return PermitSync(city={'name': 'T', 'pdf_list_url': 'https://example.test/list.pdf'})


def _patch(monkeypatch, fake, slept):
    monkeypatch.setattr(sync_permits, 'requests', fake)
    monkeypatch.setattr(sync_permits.time, 'sleep', lambda s: slept.append(s))


class TestDownloadRetry:
    def test_succeeds_after_two_failures(self, monkeypatch):
        slept = []
        fake = _FakeRequests([
            OSError('dns fail 1'),
            OSError('dns fail 2'),
            _FakeResp(b'%PDF ok'),
        ])
        _patch(monkeypatch, fake, slept)
        path = _make_sync().download_pdf_list(max_attempts=3)
        assert path == '/tmp/permit_list.pdf'
        assert fake.calls == 3        # 真的重試到第 3 次才成功
        assert slept == [5, 10]       # backoff 5s, 10s（成功前各睡一次）
        with open(path, 'rb') as f:
            assert f.read() == b'%PDF ok'

    def test_all_fail_exits_after_max_attempts(self, monkeypatch):
        slept = []
        fake = _FakeRequests([OSError('x'), OSError('x'), OSError('x')])
        _patch(monkeypatch, fake, slept)
        with pytest.raises(SystemExit) as exc:
            _make_sync().download_pdf_list(max_attempts=3)
        assert exc.value.code == 1
        assert fake.calls == 3        # 嘗試滿 max_attempts 才放棄
        assert slept == [5, 10]       # 最後一次失敗不再 sleep

    def test_first_try_success_no_retry(self, monkeypatch):
        slept = []
        fake = _FakeRequests([_FakeResp(b'%PDF first')])
        _patch(monkeypatch, fake, slept)
        path = _make_sync().download_pdf_list()
        assert fake.calls == 1        # 一次成功不重試
        assert slept == []
