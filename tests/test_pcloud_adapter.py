"""pCloud adapter：後綴白名單、短效網址、API 錯誤是 HTTP 200 + result != 0。

2026-10-01 實測：4 個非 Drive 案裡 pCloud 1 案有 44 份 PDF／135 MB，Dropbox 3 案
有 269 份／312 MB，合計 313 份——不是零星工地，值得做。本檔是第一支（pCloud），
同時把後綴白名單與「取檔前解析網址」兩個機制建起來。

兩個平台的取檔主機都是動態子網域（pCloud 回 esgp1／edef5，Dropbox 是
uc<隨機>.dl.dropboxusercontent.com），事先無法列舉，所以才需要後綴。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.source_adapters import ADAPTERS, AdapterError, SourceFile, find_adapter
from geobingan_sync.source_adapters.base import (MIN_SUFFIX_LABELS, _assert_fetchable,
                                                 _check_suffixes, fetch_pdf_bytes)
from geobingan_sync.source_adapters.pcloud import PCloudAdapter

LINK = 'https://e.pcloud.link/publink/show?code=kZyBDtZSY29KVHtTLRXvjrePhn6s0gSjGe7'
US_LINK = 'https://u.pcloud.link/publink/show?code=kZabcdefghijklmnop'


# ---------- 後綴白名單 ----------

@pytest.mark.parametrize('host', ['pcloud.com', 'esgp1.pcloud.com', 'edef5.pcloud.com',
                                  'a.b.pcloud.com'])
def test_suffix_allows_the_domain_and_its_subdomains(host):
    _assert_fetchable(f'https://{host}/x', frozenset(), frozenset({'pcloud.com'}))


@pytest.mark.parametrize('host', [
    'evil-pcloud.com',          # 只用 endswith 會放過
    'pcloud.com.evil.test',     # 後綴偽裝
    'notpcloud.com',
    'esgp1.pcloud.com.evil.test',
])
def test_suffix_does_not_allow_lookalikes(host):
    with pytest.raises(AdapterError, match='host_not_allowed'):
        _assert_fetchable(f'https://{host}/x', frozenset(), frozenset({'pcloud.com'}))


def test_exact_whitelist_is_unchanged_by_the_new_field():
    """既有 adapter 的精確比對不可悄悄變成後綴——所以刻意分開兩個欄位。"""
    _assert_fetchable('https://public-be.redsun.tw/x', frozenset({'public-be.redsun.tw'}))
    with pytest.raises(AdapterError, match='host_not_allowed'):
        _assert_fetchable('https://sub.public-be.redsun.tw/x',
                          frozenset({'public-be.redsun.tw'}))


@pytest.mark.parametrize('bad', ['com', 'tw', '', '.', '   '])
def test_too_broad_suffix_is_rejected(bad):
    """打成 'com' 會讓規則失效而且沒有任何徵兆。"""
    with pytest.raises(AdapterError, match='suffix_too_broad'):
        _check_suffixes([bad])


def test_suffix_is_normalised():
    assert _check_suffixes(['.PCloud.Com', 'pcloud.com']) == frozenset({'pcloud.com'})


def test_suffix_alone_satisfies_the_fail_closed_guard():
    """只宣告後綴也算宣告過範圍，不該被 no_allowed_hosts 擋掉。"""
    sess = _RecordingSess({'https://esgp1.pcloud.com/f': _Resp(b'%PDF-1.4 ok')})
    data = fetch_pdf_bytes('https://esgp1.pcloud.com/f', frozenset(),
                           session_factory=lambda: sess,
                           allowed_host_suffixes=frozenset({'pcloud.com'}))
    assert data == b'%PDF-1.4 ok'


def test_redirect_within_the_suffix_is_followed_but_outside_is_not():
    sess = _RecordingSess({
        'https://esgp1.pcloud.com/f': _Resp(location='https://edef5.pcloud.com/f'),
        'https://edef5.pcloud.com/f': _Resp(b'%PDF-1.4 y')})
    assert fetch_pdf_bytes('https://esgp1.pcloud.com/f', frozenset(),
                           session_factory=lambda: sess,
                           allowed_host_suffixes=frozenset({'pcloud.com'})) == b'%PDF-1.4 y'

    sess2 = _RecordingSess({'https://esgp1.pcloud.com/f': _Resp(location='https://evil.test/f')})
    with pytest.raises(AdapterError, match='host_not_allowed'):
        fetch_pdf_bytes('https://esgp1.pcloud.com/f', frozenset(),
                        session_factory=lambda: sess2,
                        allowed_host_suffixes=frozenset({'pcloud.com'}))
    assert sess2.requested == ['https://esgp1.pcloud.com/f'], '越界那一跳不可被請求'


class _Resp:
    def __init__(self, body=b'%PDF-1.4 ok', status=200, location=''):
        self._b, self.status_code = body, status
        self.headers = {'Location': location} if location else {}

    def iter_content(self, n):
        for i in range(0, len(self._b), n):
            yield self._b[i:i + n]

    def close(self): pass


class _RecordingSess:
    def __init__(self, route):
        self.requested, self._route = [], route

    def get(self, url, **kw):
        assert kw.get('allow_redirects') is False
        self.requested.append(url)
        r = self._route.get(url)
        if r is None:
            raise AssertionError(f'未預期的請求：{url}')
        return r

    def close(self): pass


# ---------- 連結比對 ----------

@pytest.mark.parametrize('url', [LINK, US_LINK,
                                 'http://e.pcloud.link/publink/show?code=kZabcdefgh'])
def test_recognised_publinks(url):
    assert find_adapter(url) is not None


@pytest.mark.parametrize('url', [
    'https://e.pcloud.link.evil.test/publink/show?code=kZabcdefgh',   # 後綴偽裝
    'https://e.pcloud.link/publink/show',                             # 沒有 code
    'https://e.pcloud.link/publink/show?code=',
    'https://e.pcloud.link/other?code=kZabcdefgh',                    # 不是 publink
    'https://e.pcloud.link:8443/publink/show?code=kZabcdefgh',        # 指定埠號
    'https://pcloud.com/publink/show?code=kZabcdefgh',                # 不是已驗證的連結主機
    'https://e.pcloud.link/publink/show?code=kZ+bad/chars',           # code 形狀不合
    '', None, 123,
])
def test_unrecognised_links(url):
    assert find_adapter(url) is None


def test_region_maps_to_the_right_api_host():
    a = PCloudAdapter()
    assert a._resolve(LINK)[0] == 'eapi.pcloud.com'
    assert a._resolve(US_LINK)[0] == 'api.pcloud.com'


# ---------- 列檔 ----------

def _tree():
    return {'result': 0, 'metadata': {'name': '觀測資料', 'isfolder': True, 'contents': [
        {'isfolder': True, 'name': '114.07', 'contents': [
            {'isfolder': False, 'name': 'a.pdf', 'fileid': 11, 'size': 100,
             'modified': 'Fri, 08 Aug 2025 07:43:31 +0000'},
            {'isfolder': False, 'name': '略過.png', 'fileid': 12, 'size': 9},
        ]},
        {'isfolder': False, 'name': 'b.pdf', 'fileid': 13, 'size': 200},
    ]}}


def test_list_files_walks_subfolders_and_keeps_paths():
    out = PCloudAdapter().list_files(LINK, fetch=lambda u: (200, '', json.dumps(_tree())))
    assert [(f.name, f.path, f.size) for f in out] == [('a.pdf', '114.07', 100),
                                                       ('b.pdf', '', 200)]


def test_listed_url_points_at_the_api_not_the_file():
    """列檔時拿不到檔案位址，先放換網址用的 API 位址。"""
    out = PCloudAdapter().list_files(LINK, fetch=lambda u: (200, '', json.dumps(_tree())))
    assert out[0].url.startswith('https://eapi.pcloud.com/getpublinkdownload?')
    assert 'fileid=11' in out[0].url
    assert out[0].allowed_hosts == frozenset({'eapi.pcloud.com'})
    assert out[0].allowed_host_suffixes == frozenset({'pcloud.com'})


@pytest.mark.parametrize('body', [
    json.dumps({'result': 7001, 'error': "Invalid link 'code'."}),   # 區域用錯，實測過
    json.dumps({'result': 0}),                                      # 沒有 metadata
    json.dumps({'result': 0, 'metadata': {'contents': []}}),        # 沒有 PDF
    json.dumps({'result': 0, 'metadata': {'contents': [
        {'isfolder': False, 'name': 'x.png', 'fileid': 1}]}}),
    'not json', '[]',
])
def test_bad_list_payloads_raise(body):
    with pytest.raises(AdapterError):
        PCloudAdapter().list_files(LINK, fetch=lambda u: (200, '', body))


def test_api_error_is_http_200_so_result_must_be_checked():
    """pCloud 的錯誤是 HTTP 200 + result != 0。不看 result 會把錯誤當成空資料夾。"""
    body = json.dumps({'result': 2000, 'error': 'Log in required.'})
    with pytest.raises(AdapterError, match='pcloud_result_2000'):
        PCloudAdapter().list_files(LINK, fetch=lambda u: (200, '', body))


# ---------- 取檔前解析網址 ----------

def _dl(hosts, path='/abc/file.pdf'):
    return json.dumps({'result': 0, 'hosts': hosts, 'path': path,
                       'expires': 'Thu, 01 Oct 2026 13:22:05 +0000'})


def _listed():
    return PCloudAdapter().list_files(LINK, fetch=lambda u: (200, '', json.dumps(_tree())))[0]


def test_resolve_download_builds_the_real_url():
    src = _listed()
    got = PCloudAdapter().resolve_download(
        src, fetch=lambda u: (200, '', _dl(['esgp1.pcloud.com', 'edef5.pcloud.com'])))
    assert got.url == 'https://esgp1.pcloud.com/abc/file.pdf'
    assert got.allowed_hosts == frozenset({'esgp1.pcloud.com'})
    assert (got.name, got.path, got.size) == (src.name, src.path, src.size)


def test_resolve_download_rejects_hosts_off_the_platform():
    """主機由 API 回應決定，不可照單全收——那正是後綴檢查要擋的事。"""
    with pytest.raises(AdapterError, match='download_host_off_platform'):
        PCloudAdapter().resolve_download(
            _listed(), fetch=lambda u: (200, '', _dl(['evil.test', 'pcloud.com.evil.test'])))


def test_resolve_download_picks_the_first_platform_host():
    got = PCloudAdapter().resolve_download(
        _listed(), fetch=lambda u: (200, '', _dl(['evil.test', 'edef5.pcloud.com'])))
    assert got.url.startswith('https://edef5.pcloud.com/')


@pytest.mark.parametrize('body', [
    _dl([], '/x'), _dl(['esgp1.pcloud.com'], 'no-leading-slash'),
    json.dumps({'result': 0, 'hosts': ['esgp1.pcloud.com']}),
    json.dumps({'result': 1900, 'error': 'nope'}),
])
def test_bad_download_payloads_raise(body):
    with pytest.raises(AdapterError):
        PCloudAdapter().resolve_download(_listed(), fetch=lambda u: (200, '', body))


def test_resolve_download_refuses_an_unexpected_api_host():
    bad = SourceFile(name='a.pdf', url='https://evil.test/getpublinkdownload?code=x&fileid=1',
                     allowed_hosts=frozenset({'evil.test'}))
    with pytest.raises(AdapterError, match='unexpected_api_host'):
        PCloudAdapter().resolve_download(bad, fetch=lambda u: (200, '', _dl(['esgp1.pcloud.com'])))


# ---------- 管線：掛勾必須真的被呼叫 ----------

def test_upload_calls_resolve_download_just_before_fetching(monkeypatch):
    """短效網址必須緊貼取檔那一刻才換。掛勾沒被呼叫的話 pCloud 整個不會動。"""
    import geobingan_sync.steps.sync_permits as sp
    order = []

    class _Adapter:
        name = 'fake'

        def resolve_download(self, src):
            order.append('resolve')
            return SourceFile(name=src.name, url='https://esgp1.pcloud.com/real.pdf',
                              allowed_hosts=frozenset({'esgp1.pcloud.com'}),
                              allowed_host_suffixes=frozenset({'pcloud.com'}))

    def _fetch(url, hosts, allowed_host_suffixes=frozenset()):
        order.append(f'fetch:{url}')
        return b'%PDF-1.4'
    monkeypatch.setattr('geobingan_sync.source_adapters.fetch_pdf_bytes', _fetch)

    ps = sp.PermitSync.__new__(sp.PermitSync)
    ps.get_or_create_subfolder = lambda *a, **k: 'sub'
    ps._get_svc = lambda: _FakeSvc()
    src = SourceFile(name='a.pdf', url='https://eapi.pcloud.com/getpublinkdownload?x',
                     allowed_hosts=frozenset({'eapi.pcloud.com'}))
    ps.upload_remote_file(src, 'target', adapter=_Adapter())
    assert order == ['resolve', 'fetch:https://esgp1.pcloud.com/real.pdf']


def test_upload_without_the_hook_uses_the_listed_url(monkeypatch):
    """沒有 resolve_download 的 adapter（例如 redsun）要走原路，不可被影響。"""
    import geobingan_sync.steps.sync_permits as sp
    seen = []

    def _fetch(url, hosts, allowed_host_suffixes=frozenset()):
        seen.append(url)
        return b'%PDF-1.4'
    monkeypatch.setattr('geobingan_sync.source_adapters.fetch_pdf_bytes', _fetch)

    class _NoHook:
        name = 'nohook'
    ps = sp.PermitSync.__new__(sp.PermitSync)
    ps._get_svc = lambda: _FakeSvc()
    src = SourceFile(name='a.pdf', url='https://public-be.redsun.tw/u/a.pdf',
                     allowed_hosts=frozenset({'public-be.redsun.tw'}))
    ps.upload_remote_file(src, 'target', adapter=_NoHook())
    assert seen == ['https://public-be.redsun.tw/u/a.pdf']


class _FakeSvc:
    def files(self):
        class _F:
            def create(self, body, media_body, fields, supportsAllDrives):
                class _E:
                    def execute(self_inner): return {'id': 'fake'}
                return _E()
        return _F()


def test_every_adapter_declares_its_download_scope():
    for a in ADAPTERS:
        hosts = getattr(a, 'download_hosts', frozenset())
        sfx = getattr(a, 'download_host_suffixes', frozenset())
        assert hosts or sfx, f'{a.name} 未宣告任何下載範圍'
