"""間接連結解析：把非 Drive 資料夾的來源再走一步（Google Sites 頁面、短網址）。

2026-09-23 實測：建管處清單 439 案中 73 案不是 Drive 資料夾，其中 30 案只要
跟著連結再走一步就落回 Drive 資料夾。這支程式吃下那 30 案；另 37 案需專屬 adapter。

安全重點：URL 來自外部文件（政府公告 PDF，內容由各承造人自行填寫），等同不可信
輸入。逐跳檢查目的地、拒絕內網位址，避免同步流程變成打內網的跳板。
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync import link_resolver as lr

FOLDER = 'https://drive.google.com/drive/folders/1N07lVUXyZH7ZjzAaZfAbmBE4a78DG'
FID = '1N07lVUXyZH7ZjzAaZfAbmBE4a78DG'
NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)


def _allow_test_hosts(host):
    """測試用主機檢查：`*.test` 放行，其餘（含 IP 字面值）仍走真實的 SSRF 邏輯。

    真實邏輯會對假主機名做 DNS 查詢而失敗，讓注入 fetch 的單元測試依賴網路。
    """
    if host.endswith('.test'):
        return True, ''
    return lr._is_public_host(host)


def _fetch(pages):
    """pages: {url: (status, location, body)}；未列出的 url 視為 404 空頁。"""
    def f(url):
        return pages.get(url, (404, '', ''))
    return f


# ---------- 三條解析路徑 ----------

def test_direct_drive_folder_needs_no_fetch():
    called = []
    res = lr.resolve_to_drive_folder(FOLDER, fetch=lambda u: called.append(u))
    assert res.ok and res.folder_id == FID and res.method == 'direct'
    assert called == [], '已經是 Drive 資料夾就不該連外'


def test_shortener_redirect_to_drive():
    pages = {'https://bit.ly/abc': (301, FOLDER, '')}
    res = lr.resolve_to_drive_folder('https://bit.ly/abc', fetch=_fetch(pages),
                                     sleep=lambda s: None)
    assert res.ok and res.folder_id == FID and res.method == 'redirect'


def test_google_sites_page_with_one_embedded_folder():
    """實測 29 案全是這種：Sites 頁面裡嵌著一個 Drive 資料夾連結。"""
    body = f'<html><body><a href="{FOLDER}">監測資料</a></body></html>'
    pages = {'https://sites.google.com/view/x': (200, '', body)}
    res = lr.resolve_to_drive_folder('https://sites.google.com/view/x', fetch=_fetch(pages))
    assert res.ok and res.folder_id == FID and res.method == 'embedded'


def test_folder_url_variants_are_recognised():
    for u in ('https://drive.google.com/drive/u/0/folders/' + FID,
              'https://drive.google.com/folders/' + FID,
              'https://drive.google.com/drive/folders/' + FID + '?usp=sharing'):
        assert lr.resolve_to_drive_folder(u).folder_id == FID, u


# ---------- 拒絕猜測 ----------

def test_two_folders_is_ambiguous_and_refused():
    """猜錯會把別人的資料夾掛到這個建案底下，比沒資料更糟。"""
    other = 'https://drive.google.com/drive/folders/ZZZZZZZZZZZZZZZZZZZZ'
    body = f'<a href="{FOLDER}">A</a><a href="{other}">B</a>'
    pages = {'https://sites.google.com/view/x': (200, '', body)}
    res = lr.resolve_to_drive_folder('https://sites.google.com/view/x', fetch=_fetch(pages))
    assert not res.ok and res.note.startswith('ambiguous:2')


def test_same_folder_twice_is_not_ambiguous():
    body = f'<a href="{FOLDER}">A</a><a href="{FOLDER}">B</a>'
    pages = {'https://sites.google.com/view/x': (200, '', body)}
    assert lr.resolve_to_drive_folder('https://sites.google.com/view/x',
                                      fetch=_fetch(pages)).folder_id == FID


def test_page_without_drive_link():
    pages = {'https://example.com/x': (200, '', '<html>沒有連結</html>')}
    res = lr.resolve_to_drive_folder('https://example.com/x', fetch=_fetch(pages))
    assert not res.ok and 'no_drive_link' in res.note


def test_fetch_error_does_not_raise():
    def boom(url):
        raise OSError('down')
    res = lr.resolve_to_drive_folder('https://example.com/x', fetch=boom)
    assert not res.ok and res.note.startswith('fetch_error:OSError')


def test_redirect_loop_bounded():
    pages = {'https://example.com/a': (302, 'https://example.com/a', '')}
    res = lr.resolve_to_drive_folder('https://example.com/a', fetch=_fetch(pages),
                                     sleep=lambda s: None)
    assert not res.ok and res.note == 'too_many_redirects'


# ---------- SSRF 防護 ----------

@pytest.mark.parametrize('url', [
    'http://127.0.0.1/admin', 'http://localhost/x', 'http://10.0.0.1/x',
    'http://192.168.1.1/x', 'http://169.254.169.254/latest/meta-data/',
])
def test_private_and_loopback_hosts_refused(url):
    """URL 來自外部文件；不擋就等於讓第三方指揮我們去打內網。"""
    called = []
    res = lr.resolve_to_drive_folder(url, fetch=lambda u: called.append(u))
    assert not res.ok and ('blocked_ip' in res.note or 'dns_fail' in res.note)
    assert called == [], '被擋下的位址不可發出請求'


def test_non_http_scheme_refused():
    res = lr.resolve_to_drive_folder('file:///etc/passwd', fetch=lambda u: 1 / 0)
    assert not res.ok and res.note.startswith('bad_scheme')


def test_redirect_into_private_ip_refused():
    """第一跳是公開網域、第二跳轉進內網——逐跳檢查才擋得住。"""
    pages = {'https://example.com/x': (302, 'http://169.254.169.254/meta', '')}
    res = lr.resolve_to_drive_folder('https://example.com/x', fetch=_fetch(pages),
                                     sleep=lambda s: None)
    assert not res.ok and ('blocked_ip' in res.note or 'dns_fail' in res.note)


# ---------- 快取 ----------

def test_cache_hit_skips_resolver():
    calls = []
    cache = {}
    r1 = lr.cached_resolve('A', FOLDER, cache, now=NOW,
                           resolver=lambda u: calls.append(u) or lr.Resolution(FID, 'direct'))
    assert r1.ok and len(calls) == 1
    r2 = lr.cached_resolve('A', FOLDER, cache, now=NOW,
                           resolver=lambda u: calls.append(u) or lr.Resolution(FID, 'direct'))
    assert r2.ok and len(calls) == 1 and 'cached' in r2.note


def test_cache_invalidated_when_source_url_changes():
    calls = []
    cache = {}
    res = lambda u: calls.append(u) or lr.Resolution(FID, 'direct')   # noqa: E731
    lr.cached_resolve('A', FOLDER, cache, now=NOW, resolver=res)
    lr.cached_resolve('A', FOLDER + '2', cache, now=NOW, resolver=res)
    assert len(calls) == 2, '來源 URL 換了就必須重新解析'


def test_cache_expires_after_refresh_days():
    calls = []
    cache = {}
    res = lambda u: calls.append(u) or lr.Resolution(FID, 'direct')   # noqa: E731
    lr.cached_resolve('A', FOLDER, cache, now=NOW, resolver=res)
    lr.cached_resolve('A', FOLDER, cache, now=NOW + timedelta(days=15), resolver=res)
    assert len(calls) == 2


def test_failures_are_cached_too():
    """37 個解不開的連結若不快取，每次同步都會再打一輪，既慢又不禮貌。"""
    calls = []
    cache = {}
    res = lambda u: calls.append(u) or lr.Resolution(None, 'none', 'no_drive_link')  # noqa: E731
    lr.cached_resolve('A', 'https://x.test/a', cache, now=NOW, resolver=res)
    lr.cached_resolve('A', 'https://x.test/a', cache, now=NOW, resolver=res)
    assert len(calls) == 1 and cache['A']['folder_id'] is None


def test_cache_roundtrip(tmp_path):
    p = str(tmp_path / 'c.json')
    assert lr.load_cache(p) == {}
    lr.save_cache({'A': {'folder_id': FID}}, p)
    assert lr.load_cache(p)['A']['folder_id'] == FID


def test_corrupt_cache_is_ignored(tmp_path):
    p = str(tmp_path / 'c.json')
    open(p, 'w').write('{壞掉的 JSON')
    assert lr.load_cache(p) == {}


# ---------- 接點：解析失敗不可中斷同步 ----------

def test_match_permits_hook_fills_folder_id(tmp_path):
    from geobingan_sync.steps.match_permits import _resolve_indirect_links
    results = {'A': {'source_url': 'https://sites.google.com/view/x', 'source_folder_id': None}}
    out = _resolve_indirect_links(results, ['A'], cache_path=str(tmp_path / 'c.json'),
                                  resolver=lambda u: lr.Resolution(FID, 'embedded'))
    assert out['A']['source_folder_id'] == FID and out['A']['source_link_method'] == 'embedded'


def test_match_permits_hook_survives_resolver_exception(tmp_path):
    from geobingan_sync.steps.match_permits import _resolve_indirect_links
    def boom(u):
        raise RuntimeError('x')
    results = {'A': {'source_url': 'https://x.test/a', 'source_folder_id': None}}
    out = _resolve_indirect_links(results, ['A'], cache_path=str(tmp_path / 'c.json'),
                                  resolver=boom)
    assert out['A']['source_folder_id'] is None      # 維持原狀，不炸


def test_relative_redirect_is_joined_against_base():
    """Location 可以是相對路徑（HTTP 規範允許）。SharePoint 實際就這樣回。

    不做 urljoin 會得到空的 scheme，被誤判成 bad_scheme——真實資料中 9 個
    cectw／ky83449379 的分享連結就是這樣被錯誤分類的。
    """
    pages = {
        'https://host.test/a': (302, '/:f:/g/personal/x', ''),
        'https://host.test/:f:/g/personal/x': (200, '', f'<a href="{FOLDER}">x</a>'),
    }
    res = lr.resolve_to_drive_folder('https://host.test/a', fetch=_fetch(pages),
                                     sleep=lambda s: None, host_check=_allow_test_hosts)
    assert res.ok and res.folder_id == FID and res.method == 'embedded'


def test_protocol_relative_redirect():
    """//host/path 形式也要正確補上協定。"""
    pages = {
        'https://host.test/a': (302, '//other.test/b', ''),
        'https://other.test/b': (200, '', f'<a href="{FOLDER}">x</a>'),
    }
    res = lr.resolve_to_drive_folder('https://host.test/a', fetch=_fetch(pages),
                                     sleep=lambda s: None, host_check=_allow_test_hosts)
    assert res.ok and res.folder_id == FID


def test_relative_redirect_still_checked_for_private_ip():
    """相對轉址合併後仍要逐跳做 SSRF 檢查，不可因為同主機就跳過。"""
    pages = {'https://host.test/a': (302, 'http://127.0.0.1/x', '')}
    res = lr.resolve_to_drive_folder('https://host.test/a', fetch=_fetch(pages),
                                     sleep=lambda s: None, host_check=_allow_test_hosts)
    assert not res.ok and 'blocked_ip' in res.note


def test_host_check_is_injectable_but_defaults_to_real_one():
    """預設必須是真的 SSRF 檢查——可注入是為了測試，不是為了讓 production 繞過。"""
    import inspect
    sig = inspect.signature(lr.resolve_to_drive_folder)
    assert sig.parameters['host_check'].default is None
    assert not lr.resolve_to_drive_folder('http://127.0.0.1/x', fetch=lambda u: 1 / 0).ok


# ---------- DNS rebinding／TOCTOU（review P1） ----------

class _FakeSock:
    def __init__(self, ip):
        self._ip = ip

    def getpeername(self):
        return (self._ip, 443)


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.1.2.3', '192.168.0.5',
                                '169.254.169.254', '172.16.0.1', '0.0.0.0'])
def test_actual_peer_ip_blocked(ip):
    """連線層守門看的是**實際連上的**對端 IP，不是先前查到的 IP。"""
    with pytest.raises(lr.BlockedAddress) as e:
        lr._assert_public_peer(_FakeSock(ip))
    assert 'blocked_peer' in str(e.value)


def test_actual_peer_public_ip_allowed():
    lr._assert_public_peer(_FakeSock('8.8.8.8'))     # 不得拋例外


def test_peername_failure_is_blocked():
    """拿不到對端位址就不能放行——未知比已知危險。"""
    class Broken:
        def getpeername(self):
            raise OSError('closed')
    with pytest.raises(lr.BlockedAddress):
        lr._assert_public_peer(Broken())


def test_dns_rebinding_first_public_then_private_is_caught(monkeypatch):
    """rebinding 情境：連線前 DNS 回公網（通過前置檢查），實際連上的是內網。

    只做連線前檢查的實作會在這裡放行——這正是 review P1 指出的 TOCTOU 缺口。
    守門必須在連線之後、送出資料之前，依實際對端 IP 判斷。
    """
    # 前置檢查：DNS 回公網 IP → 通過
    monkeypatch.setattr(lr.socket, 'getaddrinfo',
                        lambda host, port: [(2, 1, 6, '', ('93.184.216.34', 0))])
    assert lr._is_public_host('rebind.example')[0] is True

    # 實際連線：對端卻是 metadata 位址 → 連線層擋下
    with pytest.raises(lr.BlockedAddress):
        lr._assert_public_peer(_FakeSock('169.254.169.254'))


def test_guarded_connection_classes_call_the_check(monkeypatch):
    """守門真的掛在 _new_conn 上（不是只定義了函式卻沒接上）。"""
    sess = lr._guarded_session()
    adapter = sess.get_adapter('https://example.com/')
    pools = adapter.poolmanager.pool_classes_by_scheme
    for scheme in ('http', 'https'):
        conn_cls = pools[scheme].ConnectionCls
        src = conn_cls._new_conn.__code__.co_names
        assert '_assert_public_peer' in src, f'{scheme} 的連線類別沒有呼叫守門'
    sess.close()


def test_session_mounts_guarded_adapter_for_both_schemes():
    sess = lr._guarded_session()
    for url in ('http://example.com/', 'https://example.com/'):
        assert type(sess.get_adapter(url)).__name__ == '_GuardedAdapter'
    sess.close()


# ---------- proxy 繞過（review P1 第二條路徑） ----------

def test_session_ignores_environment_proxies(monkeypatch):
    """環境設了 proxy 也不得改走 proxy。

    走 proxy 時連線由 ProxyManager 建立，完全不經過 guarded connection；
    peer 又是 proxy 的 IP，檢查它沒有意義——DNS 解析與連線都在 proxy 那端，
    內網照樣到得了。
    """
    for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY'):
        monkeypatch.setenv(k, 'http://evil-proxy.invalid:8080')
    sess = lr._guarded_session()
    assert sess.trust_env is False and sess.proxies == {}
    merged = sess.merge_environment_settings('https://example.com/', {}, None, None, None)
    assert merged['proxies'] == {}, '仍會走環境 proxy'
    sess.close()


def test_proxy_manager_is_refused_loudly():
    """就算有人硬塞 proxy 也要大聲失敗，不可靜默失去守門。"""
    sess = lr._guarded_session()
    adapter = sess.get_adapter('https://example.com/')
    with pytest.raises(lr.BlockedAddress) as e:
        adapter.proxy_manager_for('http://proxy.invalid:3128')
    assert 'proxy_not_allowed' in str(e.value)
    sess.close()


def test_explicit_proxies_argument_also_blocked(monkeypatch):
    """requests 走 proxy 時會呼叫 proxy_manager_for；被擋下即無法繞過。"""
    sess = lr._guarded_session()
    adapter = sess.get_adapter('http://example.com/')
    with pytest.raises(lr.BlockedAddress):
        adapter.proxy_manager_for('http://127.0.0.1:8080')
    sess.close()
