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


def _http_get(url: str):
    """回 (status, location, body)。不自動跟隨轉址——每一跳都要重新檢查目的地。"""
    import requests
    r = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT,
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
