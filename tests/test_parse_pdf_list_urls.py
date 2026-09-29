"""清單解析要抓**任何** http(s) 連結，並把 folder id 的抽取限定在 Drive 主機。

2026-09-29 發現：舊版只抓 `https://drive.google.com` 開頭，於是 Google Sites／
SharePoint／gofile／Dropbox／Synology 等 20 幾種空間在這一步就被當成「無連結」
丟掉（實測 439 案中 71 案）。PR #95 的 link_resolver 因此只收到 2 個候選而不是
73 個，整個功能等於白做——**模組測過了，但正式管線沒餵到料**。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.steps.sync_permits import PermitSync


@pytest.fixture
def ps():
    return PermitSync(city={'name': 'T', 'pdf_list_url': 'https://x.test/l.pdf'})


# ---------- folder id 只認 Drive 主機 ----------

@pytest.mark.parametrize('url,expected', [
    ('https://drive.google.com/drive/folders/1N07lVUXyZH7ZjzAaZfAb', '1N07lVUXyZH7ZjzAaZfAb'),
    ('https://drive.google.com/drive/u/1/folders/13F89BOn5XfWGjZ-', '13F89BOn5XfWGjZ-'),
    ('https://drive.google.com/drive/mobile/folders/1v_Z4g70VEI7-', '1v_Z4g70VEI7-'),
    ('https://drive.google.com/open?id=1AbCdEfGh', '1AbCdEfGh'),
    ('https://docs.google.com/folders/1XyZ', '1XyZ'),
])
def test_drive_urls_yield_folder_id(ps, url, expected):
    assert ps.extract_folder_id_from_url(url) == expected


@pytest.mark.parametrize('url', [
    'https://example.com/share?oid=AbC123XyZ',          # oid= 也含 id=，舊版會誤抓
    'https://foo.sharepoint.com/x?id=DocLib42',
    'https://nas.example.com:5000/sharing/abc?id=share01',
    'https://sites.google.com/view/duw231107016',
    'http://gofile.me/3nCZT/Dg4yzeJWI',
    'https://www.dropbox.com/scl/fo/abc/h?rlkey=xyz&dl=0',
    'https://mega.nz/folder/SQEQVBpK#z3XxyS9',
])
def test_non_drive_urls_yield_no_folder_id(ps, url):
    """拿假的 folder id 去查 Drive，查不到還算好，查到別人的資料夾更糟。"""
    assert ps.extract_folder_id_from_url(url) is None


def test_empty_and_none_are_safe(ps):
    assert ps.extract_folder_id_from_url('') is None
    assert ps.extract_folder_id_from_url(None) is None


def test_lookalike_host_is_rejected(ps):
    """`drive.google.com.evil.test` 不是 Drive。"""
    assert ps.extract_folder_id_from_url(
        'https://drive.google.com.evil.test/drive/folders/1AbC') is None


# ---------- 清單解析抓任何 http(s) ----------

class _FakePage:
    def __init__(self, text):
        self._t = text

    def extract_text(self):
        return self._t


def _parse(ps, monkeypatch, text, tmp_path):
    import geobingan_sync.steps.sync_permits as sp

    class _Reader:
        def __init__(self, f):
            self.pages = [_FakePage(text)]
    monkeypatch.setattr(sp.pypdf, 'PdfReader', _Reader)
    f = tmp_path / 'l.pdf'
    f.write_bytes(b'%PDF-1.4')
    return ps.parse_pdf_list(str(f))


def test_sharepoint_colon_not_truncated(ps, monkeypatch, tmp_path):
    """少了 `:` 的話 `/:f:/g/...` 會在第一個冒號就被切斷。"""
    url = 'https://cectw-my.sharepoint.com/:f:/g/personal/220g_cectw_com/EttQ4E'
    out = _parse(ps, monkeypatch, f'111建字第0017號 王建築師事務所 大陸工程 {url} 下一欄中文', tmp_path)
    assert out['111建字第0017號'] == url


@pytest.mark.parametrize('url', [
    'https://sites.google.com/view/duw231107016',
    'http://gofile.me/3nCZT/Dg4yzeJWI',
    'https://www.dropbox.com/scl/fo/abc/h?rlkey=xyz&dl=0',
    'https://mega.nz/folder/SQEQVBpK#z3XxyS9',
    'https://1drv.ms/f/s!AbCdEf',
    'http://125.227.22.67:5000/sharing/rW7E3kLc5',
    'https://e.pcloud.link/publink/show?code=kZabc',
])
def test_non_drive_hosts_are_captured(ps, monkeypatch, tmp_path, url):
    """舊版把這些全丟掉，下游 resolver 因此收不到料。"""
    out = _parse(ps, monkeypatch, f'112建字第0001號 事務所 營造 {url} 中文結尾', tmp_path)
    assert out.get('112建字第0001號') == url


def test_chinese_terminates_the_url(ps, monkeypatch, tmp_path):
    """空白已被移除，邊界只能靠中文字截斷。"""
    out = _parse(ps, monkeypatch, '113建字第0015號 某事務所 某營造 https://x.test/a/b 監測資料夾', tmp_path)
    assert out['113建字第0015號'] == 'https://x.test/a/b'


def test_first_url_in_chunk_wins(ps, monkeypatch, tmp_path):
    out = _parse(ps, monkeypatch,
                 '114建字第0032號 甲 乙 https://first.test/a 中文 https://second.test/b', tmp_path)
    assert out['114建字第0032號'] == 'https://first.test/a'


def test_permit_without_any_url_is_counted_missing(ps, monkeypatch, tmp_path):
    out = _parse(ps, monkeypatch, '110建字第0001號 甲事務所 乙營造 尚未提供', tmp_path)
    assert '110建字第0001號' not in out


def test_two_permits_each_get_their_own_url(ps, monkeypatch, tmp_path):
    text = ('111建字第0100號 甲 乙 https://a.test/one 中文 '
            '111建字第0200號 丙 丁 https://b.test/two 中文')
    out = _parse(ps, monkeypatch, text, tmp_path)
    assert out['111建字第0100號'] == 'https://a.test/one'
    assert out['111建字第0200號'] == 'https://b.test/two'
