"""非 Drive 來源 adapter：白名單、payload 解析、取檔邊界，以及**管線真的有分流**。

2026-09-29 逐一探測建管處清單 440 案中 44 個非 Drive 來源，結論是超過半數
（SharePoint 16、Synology QuickConnect 9）要求登入帳號，不是寫得出 adapter 的
問題。確認公開可列檔的是通傑工程 public.redsun.tw，3 案共 104 份報表。

管線測試特別重要：PR #95 的 resolver 模組測過了、正式管線卻收不到料，整個功能
空轉。所以這裡一定要有一條測 `_resolve_or_skip_indirect` 會把 adapter 能處理的
建案**留下來**，而不是只測 adapter 自己。
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.source_adapters import (ADAPTERS, AdapterError, SourceFile,
                                            fetch_pdf_bytes, find_adapter)
from geobingan_sync.source_adapters.redsun import RedsunAdapter, _file_url
from geobingan_sync.steps.sync_permits import PermitSync

SITE = 'https://public.redsun.tw/site/23'


def _payload(files):
    return json.dumps({'data': {'attributes': {'name': 'X', 'files': {'data': files}}}})


def _f(name, url, **kw):
    return {'attributes': {'name': name, 'url': url, **kw}}


# ---------- 白名單 ----------

@pytest.mark.parametrize('url', [
    'https://public.redsun.tw/site/23',
    'http://public.redsun.tw/site/7',
    'https://PUBLIC.REDSUN.TW/site/23/',
])
def test_recognised_sources(url):
    assert find_adapter(url) is not None


@pytest.mark.parametrize('url', [
    'https://public.redsun.tw.evil.test/site/1',      # 後綴偽裝
    'https://evil.test/public.redsun.tw/site/1',      # 路徑偽裝
    'https://redsun.tw/site/1',                       # 不是已驗證的前台主機
    'http://redsun.linkmygoods.com:6082/site/10',     # 另一套部署，API 形狀未驗證
    'https://public.redsun.tw:8443/site/1',           # 指定埠號
    'https://public.redsun.tw/admin',                 # 不是 site 路徑
    'https://public.redsun.tw/site/23/../admin',
    'ftp://public.redsun.tw/site/1',
    'https://drive.google.com/drive/folders/1AbC',
    '', None, 123,
])
def test_unrecognised_sources(url):
    assert find_adapter(url) is None


def test_api_url_points_at_verified_backend():
    assert RedsunAdapter().api_url(SITE) == 'https://public-be.redsun.tw/api/sites/23?populate=*'


# ---------- payload 解析 ----------

def test_list_files_joins_relative_url_to_verified_host():
    """API 回的是相對路徑，絕對網址是前端自己接的——我們要接在**已驗證的**主機上。"""
    payload = _payload([_f('a.pdf', '/uploads/a_1.pdf', size=120, updatedAt='2026-09-14T00:23:49Z')])
    out = RedsunAdapter().list_files(SITE, fetch=lambda u: (200, '', payload))
    assert out == [SourceFile(name='a.pdf', url='https://public-be.redsun.tw/uploads/a_1.pdf',
                              path='', modified='2026-09-14T00:23:49Z', size=120,
                              allowed_hosts=frozenset({'public-be.redsun.tw'}))]


def test_non_pdf_entries_are_ignored():
    payload = _payload([_f('a.pdf', '/uploads/a.pdf'), _f('logo.png', '/uploads/logo.png'),
                        _f('x.xlsx', '/uploads/x.xlsx')])
    out = RedsunAdapter().list_files(SITE, fetch=lambda u: (200, '', payload))
    assert [f.name for f in out] == ['a.pdf']


def test_entry_without_name_or_url_is_skipped():
    payload = _payload([_f('a.pdf', '/uploads/a.pdf'), {'attributes': {'name': 'b.pdf'}},
                        {'attributes': {'url': '/uploads/c.pdf'}}])
    out = RedsunAdapter().list_files(SITE, fetch=lambda u: (200, '', payload))
    assert [f.name for f in out] == ['a.pdf']


@pytest.mark.parametrize('body,status', [
    ('not json', 200),
    (json.dumps({'data': None}), 200),
    (_payload([]), 200),                       # 沒有任何 PDF＝不可當成同步成功
    (_payload([_f('a.png', '/uploads/a.png')]), 200),
    ('{}', 404),
])
def test_bad_payloads_raise(body, status):
    with pytest.raises(AdapterError):
        RedsunAdapter().list_files(SITE, fetch=lambda u: (status, '', body))


def test_fetch_exception_becomes_adapter_error():
    def boom(_u):
        raise TimeoutError('slow')
    with pytest.raises(AdapterError):
        RedsunAdapter().list_files(SITE, fetch=boom)


# ---------- 檔案網址不得被 payload 導去別台主機 ----------

def test_file_url_relative_is_joined():
    assert _file_url('public-be.redsun.tw', '/u/a.pdf') == 'https://public-be.redsun.tw/u/a.pdf'


def test_file_url_same_host_absolute_is_kept():
    u = 'https://public-be.redsun.tw/u/a.pdf'
    assert _file_url('public-be.redsun.tw', u) == u


@pytest.mark.parametrize('href', [
    'https://evil.test/a.pdf',
    'http://169.254.169.254/latest/meta-data',
    'https://public-be.redsun.tw.evil.test/a.pdf',
    'file:///etc/passwd',
    'a.pdf',
])
def test_file_url_off_host_or_odd_scheme_is_rejected(href):
    """payload 由對方伺服器控制。照單全收絕對網址，等於讓來源決定我們去打哪台主機
    ——那正是白名單要擋的事，不能從後門放進來。"""
    with pytest.raises(AdapterError):
        _file_url('public-be.redsun.tw', href)


# ---------- 取檔邊界：逐跳驗證（review P1）----------
#
# 原本用 requests 的 allow_redirects=True，等於只驗了第一個網址：白名單主機回一個
# 302 就能把我們導去任意公網主機，而連線層守門只擋得掉內網 IP、擋不了「跑去別人家」。
# 跳數上限也是事後才數，超額的那一台早就被敲過了。

HOSTS = frozenset({'ok.test'})


class _Resp:
    def __init__(self, body=b'%PDF-1.7 ok', status=200, location=''):
        self._b, self.status_code = body, status
        self.headers = {'Location': location} if location else {}

    def iter_content(self, n):
        for i in range(0, len(self._b), n):
            yield self._b[i:i + n]

    def close(self): pass


class _RecordingSess:
    """記下**實際發出**的每一個請求網址。斷言「某一跳不得被請求」只能靠這個。"""

    def __init__(self, route):
        self.requested = []
        self._route = route          # url -> _Resp，或 callable(url) -> _Resp

    def get(self, url, **kw):
        assert kw.get('allow_redirects') is False, '必須自己逐跳跟轉址'
        self.requested.append(url)
        r = self._route(url) if callable(self._route) else self._route.get(url)
        if r is None:
            raise AssertionError(f'測試未預期的請求：{url}')
        return r

    def close(self): pass


def _fetch(route, url='https://ok.test/a.pdf', hosts=HOSTS, **kw):
    sess = _RecordingSess(route)
    try:
        data = fetch_pdf_bytes(url, hosts, session_factory=lambda: sess, **kw)
    except AdapterError as e:
        return sess, e, None
    return sess, None, data


def test_fetch_pdf_bytes_happy_path():
    sess, err, data = _fetch({'https://ok.test/a.pdf': _Resp(b'%PDF-1.7 body')})
    assert err is None and data == b'%PDF-1.7 body'
    assert sess.requested == ['https://ok.test/a.pdf']


def test_empty_allowed_hosts_is_refused():
    """沒人宣告允許範圍＝不准連，不可變成「沒設定就是不限制」。"""
    sess = _RecordingSess({})
    with pytest.raises(AdapterError, match='no_allowed_hosts'):
        fetch_pdf_bytes('https://ok.test/a.pdf', frozenset(), session_factory=lambda: sess)
    assert sess.requested == []


def test_same_host_redirect_is_followed():
    sess, err, data = _fetch({
        'https://ok.test/a.pdf': _Resp(location='https://ok.test/real/a.pdf'),
        'https://ok.test/real/a.pdf': _Resp(b'%PDF-1.7 x')})
    assert err is None and data == b'%PDF-1.7 x'
    assert sess.requested == ['https://ok.test/a.pdf', 'https://ok.test/real/a.pdf']


def test_relative_redirect_is_joined_then_validated():
    sess, err, data = _fetch({
        'https://ok.test/d/a.pdf': _Resp(location='/files/a.pdf'),
        'https://ok.test/files/a.pdf': _Resp(b'%PDF-1.7 y')},
        url='https://ok.test/d/a.pdf')
    assert err is None and data == b'%PDF-1.7 y'
    assert sess.requested[-1] == 'https://ok.test/files/a.pdf'


def test_off_host_redirect_is_not_requested():
    """核心回歸：轉去白名單外的主機，第二次請求**不得發出**。"""
    sess, err, _ = _fetch({'https://ok.test/a.pdf': _Resp(location='https://evil.test/a.pdf')})
    assert err is not None and 'host_not_allowed:evil.test' in str(err)
    assert sess.requested == ['https://ok.test/a.pdf'], '越界的那一跳不可被請求'


def test_redirect_to_lookalike_host_is_not_requested():
    sess, err, _ = _fetch({'https://ok.test/a.pdf': _Resp(location='https://ok.test.evil.test/a.pdf')})
    assert 'host_not_allowed' in str(err)
    assert len(sess.requested) == 1


@pytest.mark.parametrize('location,expect', [
    ('http://127.0.0.1/a.pdf', 'host_not_allowed'),
    ('http://169.254.169.254/latest/meta-data', 'host_not_allowed'),
    ('http://10.0.0.5/a.pdf', 'host_not_allowed'),
    ('http://10.0.0.5:8080/a.pdf', 'port_not_allowed'),   # 埠號規則先擋下，也是正確拒絕
    ('http://[::1]/a.pdf', 'host_not_allowed'),
])
def test_redirect_to_internal_address_is_not_requested(location, expect):
    """連線層守門是最後一道；主機白名單要讓它根本不必出手。"""
    sess, err, _ = _fetch({'https://ok.test/a.pdf': _Resp(location=location)})
    assert expect in str(err)
    assert sess.requested == ['https://ok.test/a.pdf']


def test_redirect_adding_a_port_is_not_requested():
    """白名單是主機名。允許改埠等於允許連到同名主機上的其他服務。"""
    sess, err, _ = _fetch({'https://ok.test/a.pdf': _Resp(location='https://ok.test:9000/a.pdf')})
    assert 'port_not_allowed:9000' in str(err)
    assert sess.requested == ['https://ok.test/a.pdf']


@pytest.mark.parametrize('location', ['file:///etc/passwd', 'ftp://ok.test/a.pdf',
                                      'gopher://ok.test/a'])
def test_redirect_changing_scheme_is_not_requested(location):
    sess, err, _ = _fetch({'https://ok.test/a.pdf': _Resp(location=location)})
    assert 'bad_scheme' in str(err) or 'host_not_allowed' in str(err)
    assert sess.requested == ['https://ok.test/a.pdf']


def test_hop_cap_counts_requests_and_stops_before_exceeding():
    """跳數上限算的是**總請求數**，而且要在發請求前檢查。

    max_hops=5 → 最多發出 5 次請求；第 6 跳不得被請求。
    """
    def loop(url):
        n = int(url.rsplit('/', 1)[1])
        return _Resp(location=f'https://ok.test/{n + 1}')
    sess, err, _ = _fetch(loop, url='https://ok.test/1')
    assert 'too_many_hops' in str(err)
    assert len(sess.requested) == 5, f'實際發出 {len(sess.requested)} 次'
    assert 'https://ok.test/6' not in sess.requested


def test_hop_cap_is_configurable_and_respected():
    def loop(url):
        return _Resp(location='https://ok.test/next')
    sess, err, _ = _fetch(loop, max_hops=2)
    assert 'too_many_hops' in str(err) and len(sess.requested) == 2


def test_oversize_raises_instead_of_truncating():
    """截斷會把半份壞掉的 PDF 放進監測報告資料夾，比缺一份更糟——
    後續解析拿到壞檔，錯誤會一路往下傳。"""
    sess, err, _ = _fetch({'https://ok.test/a.pdf': _Resp(b'%PDF-1.7' + b'x' * 5000)},
                          max_bytes=8)
    assert 'too_large' in str(err)


def test_non_pdf_content_is_rejected():
    """不信 Content-Type，只信內容本身。對方回什麼標頭不由我們決定。"""
    sess, err, _ = _fetch({'https://ok.test/a.pdf':
                           _Resp('<!doctype html><html>登入'.encode('utf-8'))})
    assert 'not_pdf' in str(err)


def test_non_200_is_rejected():
    sess, err, _ = _fetch({'https://ok.test/a.pdf': _Resp(status=403)})
    assert 'http_403' in str(err)


def test_initial_url_off_host_is_never_requested():
    sess, err, _ = _fetch({}, url='https://evil.test/a.pdf')
    assert 'host_not_allowed:evil.test' in str(err)
    assert sess.requested == []


def test_adapter_declares_download_hosts():
    """adapter 沒宣告 download_hosts，SourceFile 就帶不出白名單，取檔會 fail-closed。"""
    for a in ADAPTERS:
        assert getattr(a, 'download_hosts', None), f'{a.name} 未宣告 download_hosts'


def test_listed_files_carry_allowed_hosts():
    payload = _payload([_f('a.pdf', '/uploads/a.pdf')])
    out = RedsunAdapter().list_files(SITE, fetch=lambda u: (200, '', payload))
    assert out[0].allowed_hosts == frozenset({'public-be.redsun.tw'})


# ---------- 管線：adapter 能處理的必須真的進到同步 ----------

@pytest.fixture
def ps():
    return PermitSync(city={'name': 'T', 'pdf_list_url': 'https://x.test/l.pdf'})


def test_pipeline_keeps_adapter_sources(ps):
    """PR #95 的教訓：模組測過了、管線沒餵到料，功能等於白做。"""
    mapping = {
        'A': 'https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345',
        'B': SITE,                                   # adapter 處理
        'C': 'https://cectw-my.sharepoint.com/:f:/g/personal/x/Ett',   # 無 adapter
    }
    def _never(*a, **k):
        raise AssertionError('有 adapter 的不該再去跑連結解析')
    kept = ps._resolve_or_skip_indirect(dict(mapping), resolver=_never,
                                        cache_path='/dev/null')
    assert 'B' in kept and kept['B'] == SITE, 'adapter 能處理的不可被剔除'
    assert 'A' in kept
    assert 'C' not in kept


def test_pipeline_does_not_resolve_adapter_sources(ps, capsys):
    kept = ps._resolve_or_skip_indirect({'B': SITE}, cache_path='/dev/null')
    assert kept == {'B': SITE}
    assert 'adapter' in capsys.readouterr().out


# ---------- sync_permit 分流與逐檔容錯 ----------

class _FakeAdapter:
    name = 'fake'

    def __init__(self, files, err=None):
        self._files, self._err = files, err

    def matches(self, url): return True

    def list_files(self, url):
        if self._err:
            raise self._err
        return self._files


def _wire(ps, monkeypatch, adapter, uploads, fail_on=()):
    import geobingan_sync.source_adapters as sa
    monkeypatch.setattr(sa, 'find_adapter', lambda u: adapter)
    monkeypatch.setattr(ps, 'preload_target_files', lambda *a, **k: None)
    monkeypatch.setattr(ps, 'check_file_exists', lambda *a, **k: False)
    monkeypatch.setattr(ps, 'save_state', lambda *a, **k: None)

    def _upload(src, target):
        if src.name in fail_on:
            raise AdapterError('boom')
        uploads.append(src.name)
        return f'id-{src.name}', target
    monkeypatch.setattr(ps, 'upload_remote_file', _upload)


def test_sync_permit_routes_to_adapter(ps, monkeypatch):
    files = [SourceFile('a.pdf', 'https://x.test/a.pdf'),
             SourceFile('b.pdf', 'https://x.test/b.pdf')]
    uploads = []
    _wire(ps, monkeypatch, _FakeAdapter(files), uploads)
    ps.sync_permit('111建字第0001號', SITE, 'target1')
    assert uploads == ['a.pdf', 'b.pdf']


def test_one_bad_file_does_not_abort_the_permit(ps, monkeypatch):
    """這條路走外部網站，逾時與格式異常是常態不是例外。"""
    files = [SourceFile(n, f'https://x.test/{n}') for n in ('a.pdf', 'bad.pdf', 'c.pdf')]
    uploads = []
    _wire(ps, monkeypatch, _FakeAdapter(files), uploads, fail_on={'bad.pdf'})
    ps.sync_permit('111建字第0002號', SITE, 'target1')
    assert uploads == ['a.pdf', 'c.pdf']
    assert any('1 個檔案取得失敗' in e['error'] for e in ps.state['errors'])


def test_list_failure_is_recorded_not_raised(ps, monkeypatch):
    _wire(ps, monkeypatch, _FakeAdapter([], err=AdapterError('http_500')), [])
    ps.sync_permit('111建字第0003號', SITE, 'target1')
    assert any('http_500' in e['error'] for e in ps.state['errors'])


def test_already_synced_files_are_skipped(ps, monkeypatch):
    files = [SourceFile('a.pdf', 'https://x.test/a.pdf')]
    uploads = []
    _wire(ps, monkeypatch, _FakeAdapter(files), uploads)
    monkeypatch.setattr(ps, 'check_file_exists', lambda *a, **k: True)
    ps.sync_permit('111建字第0004號', SITE, 'target1')
    assert uploads == []


def test_registry_has_no_duplicate_adapter_names():
    names = [a.name for a in ADAPTERS]
    assert len(names) == len(set(names))
