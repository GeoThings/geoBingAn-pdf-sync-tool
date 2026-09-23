"""把「不是 Drive 資料夾」的來源連結再走一步，解析回我們已經會處理的 Drive 資料夾。

為什麼（2026-09-23 實測）：建管處清單 439 案中，73 案的連結不是 Drive 資料夾，
我方完全未涵蓋。逐一探測後，其中 **30 案只要再走一步就會落回 Drive 資料夾**——
29 案是 Google Sites 頁面裡嵌著一個 Drive 連結，1 案是短網址轉址過去。
一支通用解析器就能把可驗證母體從 366 案推到 400 案（83% → 91%），
比為每一種空間寫專屬 adapter 划算得多。

附帶價值：那 29 個 Sites 頁面背後的 Drive 資料夾，目前完全沒被納入失效監控。

安全：**URL 來自外部文件**（政府公告的 PDF，內容由各承造人填寫），等同不可信輸入。
逐跳檢查轉址目的地、拒絕內網／回環／link-local 位址、限制大小與跳數，
避免把同步流程變成打內網的跳板。

防 DNS rebinding／TOCTOU（review P1）：光在連線「之前」查一次 DNS 並驗證 IP 是不夠的
——`requests` 實際連線時會**再查一次**，攻擊者用低 TTL 讓第一次回公網、第二次回
169.254.169.254 就繞過了。所以真正的守門在 `_GuardedHTTPConnection`：TCP 連上之後、
**送出任何資料之前**，用 `socket.getpeername()` 檢查**實際連到的對端 IP**。
連線前的 `_is_public_host()` 保留為第一道，可在不連線的情況下擋掉明顯的內網目標。
"""
import ipaddress
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple
from urllib.parse import urljoin, urlparse

CACHE_FILE = './state/link_resolution.json'
MAX_REDIRECTS = 5
MAX_BYTES = 2 * 1024 * 1024        # 頁面只取前 2MB，避免把大檔讀進記憶體
TIMEOUT = 20
REFRESH_DAYS = 14                  # 解析結果保鮮期；來源 URL 變更則立即失效
USER_AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/128.0 Safari/537.36 geoBingAn-pdf-sync')

_FOLDER_RE = re.compile(r'drive\.google\.com/(?:drive/)?(?:u/\d+/)?folders/([A-Za-z0-9_-]{15,})')


@dataclass
class Resolution:
    folder_id: Optional[str]
    method: str          # direct | redirect | embedded | none
    note: str = ''

    @property
    def ok(self) -> bool:
        return bool(self.folder_id)


def _is_public_host(host: str) -> Tuple[bool, str]:
    """把主機名解析成 IP，拒絕內網／回環／link-local／保留位址。

    URL 來自外部文件，不擋的話同步流程會變成打內網的跳板（SSRF）。
    解析不出來也拒絕：連不到的位址沒有解析價值，而未知比已知危險。
    """
    if not host:
        return False, 'no_host'
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        return False, f'dns_fail:{type(e).__name__}'
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, f'blocked_ip:{ip}'
    return True, ''


def _extract_folder_id(url: str) -> Optional[str]:
    m = _FOLDER_RE.search(url or '')
    return m.group(1) if m else None


def resolve_to_drive_folder(url: str, fetch: Callable = None, sleep: Callable = None,
                            host_check: Callable = None) -> Resolution:
    """回 Resolution。純網路操作集中在 fetch，供測試注入。

    順序：已是 Drive 資料夾 → 逐跳跟隨轉址 → 抓頁面找內嵌 Drive 資料夾。
    內嵌**必須恰好一個**；兩個以上視為有歧義而放棄——猜錯會把別人的資料夾
    掛到這個建案底下，比沒資料更糟（實測 29 案皆為 1 個）。
    """
    if not url:
        return Resolution(None, 'none', 'empty_url')
    fid = _extract_folder_id(url)
    if fid:
        return Resolution(fid, 'direct')

    fetch = fetch or _http_get
    sleep = sleep or time.sleep
    # host_check 可注入：否則即使注入了 fetch，單元測試仍會打真實 DNS
    host_check = host_check or _is_public_host
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        p = urlparse(current)
        if p.scheme not in ('http', 'https'):
            return Resolution(None, 'none', f'bad_scheme:{p.scheme}')
        ok, why = host_check(p.hostname or '')
        if not ok:
            return Resolution(None, 'none', why)
        try:
            status, location, body = fetch(current)
        except Exception as e:                      # noqa: BLE001 — 解析失敗不可中斷同步
            return Resolution(None, 'none', f'fetch_error:{type(e).__name__}')
        if status in (301, 302, 303, 307, 308) and location:
            # Location 可以是相對路徑（HTTP 規範允許）。SharePoint 就是這樣回的：
            # 實測 9 個 cectw/ky83449379 的分享連結轉到 `/:f:/g/personal/...`，
            # 不做 urljoin 就會得到空的 scheme 而誤判為 bad_scheme。
            nxt = urljoin(current, location)
            fid = _extract_folder_id(nxt)
            if fid:
                return Resolution(fid, 'redirect', f'hop{hop + 1}')
            current = nxt
            sleep(0.2)
            continue
        fid = _extract_folder_id(current)
        if fid:
            return Resolution(fid, 'redirect', f'hop{hop}')
        found = list(dict.fromkeys(_FOLDER_RE.findall(body or '')))
        if len(found) == 1:
            return Resolution(found[0], 'embedded')
        if len(found) > 1:
            return Resolution(None, 'none', f'ambiguous:{len(found)}')
        return Resolution(None, 'none', f'no_drive_link:http{status}')
    return Resolution(None, 'none', 'too_many_redirects')


class BlockedAddress(Exception):
    """實際連到的對端位址不被允許（內網／回環／link-local）。"""


def _assert_public_peer(sock) -> None:
    """檢查**實際連上的**對端 IP。這是防 DNS rebinding 的唯一可靠位置。

    在 `_new_conn()` 之後呼叫：TCP 已建立但尚未送出任何請求資料，
    所以被擋下時不會把 Host 標頭或路徑洩漏給內網服務。
    """
    try:
        peer = sock.getpeername()[0]
    except OSError as e:
        raise BlockedAddress(f'peer_unknown:{type(e).__name__}') from e
    ip = ipaddress.ip_address(peer)
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        raise BlockedAddress(f'blocked_peer:{ip}')


def _guarded_session():
    """requests session，連線層強制檢查實際對端 IP。"""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

    class _GuardedHTTPConnection(HTTPConnection):
        def _new_conn(self):
            sock = super()._new_conn()
            _assert_public_peer(sock)
            return sock

    class _GuardedHTTPSConnection(HTTPSConnection):
        def _new_conn(self):
            sock = super()._new_conn()
            _assert_public_peer(sock)      # TLS 握手之前就擋掉
            return sock

    class _GuardedHTTPPool(HTTPConnectionPool):
        ConnectionCls = _GuardedHTTPConnection

    class _GuardedHTTPSPool(HTTPSConnectionPool):
        ConnectionCls = _GuardedHTTPSConnection

    class _GuardedAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {
                'http': _GuardedHTTPPool, 'https': _GuardedHTTPSPool}

        def proxy_manager_for(self, proxy, **kwargs):
            # 走 proxy 時連線由 ProxyManager 建立，**完全不經過**上面的守門類別；
            # 而且 peer 會是 proxy 的 IP，檢查它也沒有意義——真正的 DNS 解析與
            # 連線都發生在 proxy 那一端，內網照樣到得了（review P1 第二條路徑）。
            # 這支 resolver 不需要 proxy，所以寧可大聲失敗，也不要靜默失去防護。
            raise BlockedAddress(f'proxy_not_allowed:{proxy}')

    sess = requests.Session()
    # trust_env=False：不讀 HTTP_PROXY／HTTPS_PROXY／NO_PROXY／.netrc。
    # 預設 True 時，只要環境設了 proxy，連線就會繞過守門。
    sess.trust_env = False
    sess.proxies = {}
    adapter = _GuardedAdapter()
    sess.mount('http://', adapter)
    sess.mount('https://', adapter)
    return sess


def _http_get(url: str):
    """回 (status, location, body)。不自動跟隨轉址——每一跳都要重新檢查目的地。"""
    sess = _guarded_session()
    r = sess.get(url, headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT,
                 allow_redirects=False, stream=True)
    loc = r.headers.get('Location', '')
    body = ''
    if not loc:
        chunks, total = [], 0
        for c in r.iter_content(8192, decode_unicode=False):
            chunks.append(c)
            total += len(c)
            if total >= MAX_BYTES:
                break
        body = b''.join(chunks).decode(r.encoding or 'utf-8', errors='replace')
    r.close()
    sess.close()
    return r.status_code, loc, body


# ---------- 快取：來源 URL 沒變就不要每次同步都去敲別人的網站 ----------

def load_cache(path: str = CACHE_FILE) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (ValueError, OSError):
        return {}


def save_cache(cache: dict, path: str = CACHE_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp.{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def cached_resolve(permit: str, url: str, cache: dict, now: datetime = None,
                   resolver: Callable = None, refresh_days: int = REFRESH_DAYS) -> Resolution:
    """快取版。來源 URL 變更、或超過保鮮期，才重新解析。

    失敗結果也快取（較短的保鮮期由呼叫端決定要不要調）——否則每次同步都會對
    37 個解不開的連結重打一輪，既慢又不禮貌。
    """
    now = now or datetime.now(timezone.utc)
    ent = cache.get(permit)
    if ent and ent.get('source_url') == url:
        try:
            at = datetime.fromisoformat(ent['resolved_at'])
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if now - at < timedelta(days=refresh_days):
                return Resolution(ent.get('folder_id'), ent.get('method', 'cache'),
                                  ent.get('note', '') + '|cached')
        except (KeyError, ValueError):
            pass
    res = (resolver or resolve_to_drive_folder)(url)
    cache[permit] = {'source_url': url, 'folder_id': res.folder_id,
                     'method': res.method, 'note': res.note,
                     'resolved_at': now.isoformat()}
    return res
