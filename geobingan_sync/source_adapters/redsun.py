"""通傑工程「公開資訊平台」（public.redsun.tw）adapter。

這個平台是 Strapi：頁面是 SPA，檔案清單在 ``public-be.redsun.tw`` 的 REST API，
完全公開、不需登入。2026-09-29 實測 3 個建案共 104 份報表全部列得出來。

主機為何寫死在允許清單（而不是從來源 URL 推導）：來源 URL 出自政府公告 PDF，
內容由各承造人自行填寫＝不可信輸入。若照 ``public.X`` → ``public-be.X`` 的規則
推導，任何人只要在清單裡填一個自家網域，就能指定我們去打哪台主機。adapter 只
認我們驗證過的平台，新增平台要有人看過。
"""
import json
import re
from typing import List
from urllib.parse import urlparse

from .base import AdapterError, SourceFile

#: 已驗證的前台主機 → 後端 API 主機。新增前台要先實測過 API 形狀。
_HOSTS = {'public.redsun.tw': 'public-be.redsun.tw'}

_SITE_RE = re.compile(r'^/site/(\d+)/?$')


def _file_url(api_host: str, href: str) -> str:
    """把 payload 裡的檔案位址接成絕對網址，且**只准指向已驗證的那台主機**。

    API 回的是相對路徑（``/uploads/xxx.pdf``），絕對網址是前端自己接的。
    payload 由對方伺服器控制，若照單全收絕對網址，等於讓來源決定我們去打哪台
    主機——那正是 adapter 白名單想擋的事，不能從後門放進來。
    """
    if href.startswith('/'):
        return f'https://{api_host}{href}'
    if href.startswith(('http://', 'https://')):
        if (urlparse(href).hostname or '').lower() != api_host:
            raise AdapterError(f'file_url_off_host:{href!r}')
        return href
    raise AdapterError(f'unexpected_file_url:{href!r}')


def _parse(url: str):
    try:
        u = urlparse(url)
    except Exception:                                       # noqa: BLE001
        return None
    if u.scheme not in ('http', 'https') or u.port:
        return None
    api_host = _HOSTS.get((u.hostname or '').lower())
    if not api_host:
        return None
    m = _SITE_RE.match(u.path or '')
    return (api_host, m.group(1)) if m else None


class RedsunAdapter:
    name = 'redsun'

    def matches(self, url: str) -> bool:
        return _parse(url) is not None

    def _resolve(self, url: str):
        parsed = _parse(url)
        if not parsed:
            raise AdapterError(f'not_a_redsun_site_url:{url}')
        return parsed

    def api_url(self, url: str) -> str:
        api_host, site_id = self._resolve(url)
        return f'https://{api_host}/api/sites/{site_id}?populate=*'

    def list_files(self, url: str, fetch=None) -> List[SourceFile]:
        from geobingan_sync.link_resolver import _http_get
        fetch = fetch or _http_get
        api_host, _site_id = self._resolve(url)
        api = self.api_url(url)
        try:
            status, _loc, body = fetch(api)
        except Exception as e:                              # noqa: BLE001
            raise AdapterError(f'{type(e).__name__}:{e}') from e
        if status != 200:
            raise AdapterError(f'http_{status}')
        try:
            attrs = json.loads(body)['data']['attributes']
        except Exception as e:                              # noqa: BLE001
            raise AdapterError(f'bad_payload:{type(e).__name__}') from e

        out = []
        for entry in (attrs.get('files') or {}).get('data') or []:
            a = entry.get('attributes') or {}
            name, href = a.get('name'), a.get('url')
            if not name or not href:
                continue
            if not str(name).lower().endswith('.pdf'):
                # 只收監測報表。平台上若日後放了圖片或試算表，不該被當成報告同步走。
                continue
            out.append(SourceFile(
                name=str(name), url=_file_url(api_host, str(href)),
                modified=str(a.get('updatedAt') or a.get('createdAt') or ''),
                size=int(a.get('size') or 0)))
        if not out:
            raise AdapterError('no_pdf_in_payload')
        return out
