"""resolve_list_pdf_url：從建管處發布頁動態解析最新清單 PDF 連結。

政府改版時會把清單換成新 relfile 路徑，寫死 pdf_list_url 會抓到舊版；此函式抓
發布頁、取出當前 Download.ashx PDF 連結。任何失敗回 None，由呼叫端 fallback。
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import geobingan_sync.steps.sync_permits as sp
from geobingan_sync.steps.sync_permits import resolve_list_pdf_url


def _b64(s):
    return base64.b64encode(s.encode('utf-8')).decode('ascii')


class _Resp:
    def __init__(self, text='', exc=None):
        self._text = text
        self._exc = exc

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        if self._exc:
            raise self._exc


def _patch_get(monkeypatch, resp):
    monkeypatch.setattr(sp.requests, 'get', lambda *a, **k: resp)


def test_resolves_download_ashx_pdf_link(monkeypatch):
    fname = '表單回復_1150902.pdf'
    href = f'https://www-ws.gov.taipei/Download.ashx?u={_b64("/001/Upload/845/relfile/62922/139249/abc.pdf")}&amp;n={_b64(fname)}&amp;icon=..pdf'
    html = f'<html><body><a href="/cp.aspx?n=x">別的</a><a href="{href}">pdf(610 KB)</a></body></html>'
    _patch_get(monkeypatch, _Resp(text=html))

    got = resolve_list_pdf_url('https://cmo.gov.taipei/cp.aspx?n=DDC36EA7C18D67F2')
    assert got is not None
    url, name = got
    # &amp; 需還原、連結需為絕對網址、指向 Download.ashx
    assert 'Download.ashx' in url and '&amp;' not in url
    assert url.startswith('https://www-ws.gov.taipei/')
    assert name == fname


def test_returns_none_when_no_pdf_link(monkeypatch):
    html = '<html><body><a href="/cp.aspx?n=x">導覽</a><a href="https://x/Download.ashx?u=' + _b64('/p/f.xlsx') + '&n=' + _b64('某表.xlsx') + '">xlsx</a></body></html>'
    _patch_get(monkeypatch, _Resp(text=html))
    assert resolve_list_pdf_url('https://cmo.gov.taipei/cp.aspx?n=x') is None


def test_returns_none_on_http_error(monkeypatch):
    _patch_get(monkeypatch, _Resp(exc=Exception('boom')))
    assert resolve_list_pdf_url('https://cmo.gov.taipei/cp.aspx?n=x') is None


def test_returns_none_on_request_exception(monkeypatch):
    def _boom(*a, **k):
        raise Exception('network down')
    monkeypatch.setattr(sp.requests, 'get', _boom)
    assert resolve_list_pdf_url('https://cmo.gov.taipei/cp.aspx?n=x') is None
