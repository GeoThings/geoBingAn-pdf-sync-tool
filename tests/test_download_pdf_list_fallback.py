"""download_pdf_list 的候選 fallback 行為（PR #78 review P1）。

動態解析的清單 URL 可能解析成功卻已失效（404/500 或回傳 HTTP 200 的 HTML
錯誤頁）。此時須：(1)raise_for_status + %PDF 驗證擋掉錯誤頁；(2)動態 URL
重試耗盡後改用靜態 pdf_list_url，而非卡死或把錯誤頁寫成 PDF。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import geobingan_sync.steps.sync_permits as sp
from geobingan_sync.steps.sync_permits import PermitSync

DYN = 'http://dyn.example/list.pdf'
STATIC = 'http://static.example/list.pdf'
GOOD_PDF = b'%PDF-1.7\n...fake construction list...'


class _FakeResp:
    def __init__(self, content=b'', status=200):
        self.content = content
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise Exception(f'HTTP {self._status}')


def _ps():
    return PermitSync(city={'name': '測試', 'list_page_url': 'http://page', 'pdf_list_url': STATIC})


def _setup(monkeypatch, router):
    # 動態解析固定回傳 DYN；requests.get 依 url 路由
    monkeypatch.setattr(sp, 'resolve_list_pdf_url', lambda page, **k: (DYN, 'f.pdf'))
    monkeypatch.setattr(sp.requests, 'get', lambda url, **k: router(url))
    monkeypatch.setattr(sp.time, 'sleep', lambda *a, **k: None)


def test_falls_back_to_static_when_dynamic_404(monkeypatch, tmp_path):
    def router(url):
        return _FakeResp(status=404) if url == DYN else _FakeResp(content=GOOD_PDF)
    _setup(monkeypatch, router)

    dest = str(tmp_path / 'list.pdf')
    path = _ps().download_pdf_list(max_attempts=1, dest=dest)
    assert path == dest                          # 不得寫進正式路徑
    assert open(path, 'rb').read() == GOOD_PDF   # 拿到靜態版，不是錯誤頁


def test_rejects_html_error_page_http200(monkeypatch, tmp_path):
    def router(url):
        # 動態 URL 回 200 但內容是 HTML 錯誤頁（非 PDF）
        return _FakeResp(content=b'<html>404 not found</html>', status=200) if url == DYN else _FakeResp(content=GOOD_PDF)
    _setup(monkeypatch, router)

    path = _ps().download_pdf_list(max_attempts=1, dest=str(tmp_path / 'list.pdf'))
    assert open(path, 'rb').read() == GOOD_PDF


def test_exits_when_all_candidates_fail(monkeypatch, tmp_path):
    def router(url):
        return _FakeResp(status=500)
    _setup(monkeypatch, router)

    with pytest.raises(SystemExit):
        _ps().download_pdf_list(max_attempts=1, dest=str(tmp_path / 'list.pdf'))


def test_never_writes_to_production_path_by_default(monkeypatch, tmp_path):
    """回歸：測試不得寫進 PERMIT_LIST_PATH（正式落地路徑）。

    2026-09-16：這些測試原本寫死 /tmp/permit_list.pdf，在跑過測試的機器上留下
    37 bytes 假清單，導致以該檔做的人工判讀誤判某建案已從政府清單下架。
    """
    import geobingan_sync.steps.sync_permits as sp
    prod = tmp_path / 'production_list.pdf'
    monkeypatch.setattr(sp, 'PERMIT_LIST_PATH', str(prod))
    _setup(monkeypatch, lambda url: _FakeResp(content=GOOD_PDF))

    dest = str(tmp_path / 'injected.pdf')
    path = _ps().download_pdf_list(max_attempts=1, dest=dest)
    assert path == dest
    assert not prod.exists(), '注入 dest 時不可碰正式路徑'


def test_defaults_to_production_path_when_dest_omitted(monkeypatch, tmp_path):
    """正式流程（不帶 dest）仍落在設定的路徑，行為不變。"""
    import geobingan_sync.steps.sync_permits as sp
    prod = tmp_path / 'production_list.pdf'
    monkeypatch.setattr(sp, 'PERMIT_LIST_PATH', str(prod))
    _setup(monkeypatch, lambda url: _FakeResp(content=GOOD_PDF))

    assert _ps().download_pdf_list(max_attempts=1) == str(prod)
    assert prod.read_bytes() == GOOD_PDF
