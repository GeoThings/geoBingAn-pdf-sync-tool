"""pCloud 公開連結 adapter。

pCloud 有文件化的公開 API，不需要跑 JS、不需要逆向：

1. ``showpublink?code=<code>`` 回整棵資料夾樹（``metadata.contents`` 遞迴），
   含檔名、fileid、大小、修改時間。
2. ``getpublinkdownload?code=<code>&fileid=<id>`` 回 ``{hosts: [...], path: ...}``，
   組成 ``https://<hosts[0]><path>`` 才是真正的檔案位址。

兩個設計上的重點：

**取檔網址是短效的。** 實測 ``expires`` 大約一小時。所以不能在 list_files 就換——
大批次的後段會全部過期。改用 ``resolve_download()`` 掛勾，緊貼下載那一刻才換。

**取檔主機事先無法列舉。** 實測回 ``esgp1.pcloud.com``／``edef5.pcloud.com``，
會變。所以宣告的是**後綴** ``pcloud.com``（等於該網域或其子網域），而不是精確清單。
那把信任邊界從「某一台主機」放寬到「pCloud 自己的網域」——仍然擋著「轉址到任意
公網主機」，逐跳驗證、禁 proxy、連線層 IP 檢查全部保留。

區域：``e.pcloud.link`` 是歐洲、``u.pcloud.link`` 是美國，各自對應不同 API 主機。
用錯會回 ``result 7001 Invalid link 'code'``（實測過）。
"""
import json
import re
from typing import List
from urllib.parse import urlparse, parse_qs

from .base import AdapterError, SourceFile

#: 已驗證的公開連結主機 → API 主機。新增區域前要先實測 showpublink 回得出東西。
_API_BY_HOST = {
    'e.pcloud.link': 'eapi.pcloud.com',
    'u.pcloud.link': 'api.pcloud.com',
}

#: 取檔主機的後綴。pCloud 的檔案由動態子網域發送（esgp1／edef5…），無法事先列舉。
DOWNLOAD_HOST_SUFFIXES = frozenset({'pcloud.com'})

_CODE_RE = re.compile(r'^[A-Za-z0-9_-]{8,128}$')


def _parse(url: str):
    """回 (api_host, code) 或 None。"""
    try:
        u = urlparse(url)
    except Exception:                                       # noqa: BLE001
        return None
    if u.scheme not in ('http', 'https') or u.port:
        return None
    api_host = _API_BY_HOST.get((u.hostname or '').lower())
    if not api_host:
        return None
    if not (u.path or '').rstrip('/').endswith('/publink/show'):
        return None
    code = (parse_qs(u.query or '').get('code') or [''])[0]
    # code 會被組進 API 的 query string，形狀先收緊，不讓奇怪字元流進去
    return (api_host, code) if _CODE_RE.match(code) else None


def _walk(node, path=''):
    """遞迴展開資料夾樹，回 [(相對路徑, 檔案 dict)]。"""
    out = []
    for child in node.get('contents') or []:
        if child.get('isfolder'):
            sub = f"{path}/{child.get('name')}" if path else str(child.get('name') or '')
            out += _walk(child, sub)
        else:
            out.append((path, child))
    return out


class PCloudAdapter:
    name = 'pcloud'
    #: 列檔與換取檔網址都打 API 主機，那是精確比對；檔案本體走後綴。
    download_hosts = frozenset(_API_BY_HOST.values())
    download_host_suffixes = DOWNLOAD_HOST_SUFFIXES

    def matches(self, url: str) -> bool:
        return _parse(url) is not None

    def _resolve(self, url: str):
        parsed = _parse(url)
        if not parsed:
            raise AdapterError(f'not_a_pcloud_publink:{url}')
        return parsed

    def _api(self, api_host: str, method: str, params: str, fetch) -> dict:
        api = f'https://{api_host}/{method}?{params}'
        try:
            status, _loc, body = fetch(api)
        except Exception as e:                              # noqa: BLE001
            raise AdapterError(f'{type(e).__name__}:{e}') from e
        if status != 200:
            raise AdapterError(f'http_{status}:{method}')
        try:
            data = json.loads(body)
        except Exception as e:                              # noqa: BLE001
            raise AdapterError(f'bad_payload:{method}:{type(e).__name__}') from e
        if not isinstance(data, dict):
            raise AdapterError(f'bad_payload:{method}:not_an_object')
        # pCloud 的錯誤是 HTTP 200 + result != 0，不看 result 會把錯誤當成空資料夾
        if data.get('result') != 0:
            raise AdapterError(f'pcloud_result_{data.get("result")}:{str(data.get("error"))[:60]}')
        return data

    def list_files(self, url: str, fetch=None) -> List[SourceFile]:
        from geobingan_sync.link_resolver import _http_get
        fetch = fetch or _http_get
        api_host, code = self._resolve(url)
        data = self._api(api_host, 'showpublink', f'code={code}', fetch)
        md = data.get('metadata')
        if not isinstance(md, dict):
            raise AdapterError('no_metadata')

        out = []
        for rel, f in _walk(md):
            name, fid = f.get('name'), f.get('fileid')
            if not name or fid is None:
                continue
            if not str(name).lower().endswith('.pdf'):
                continue                                    # 只收監測報表
            out.append(SourceFile(
                name=str(name),
                # 先放換網址用的 API 位址；resolve_download 會在取檔前換成真正的檔案位址。
                url=f'https://{api_host}/getpublinkdownload?code={code}&fileid={int(fid)}',
                path=rel,
                modified=str(f.get('modified') or ''),
                size=int(f.get('size') or 0),
                allowed_hosts=frozenset({api_host}),
                allowed_host_suffixes=self.download_host_suffixes,
            ))
        if not out:
            raise AdapterError('no_pdf_in_publink')
        return out

    def resolve_download(self, src: SourceFile, fetch=None) -> SourceFile:
        """把 API 位址換成真正的檔案位址。**緊貼取檔那一刻呼叫**（網址短效）。"""
        from geobingan_sync.link_resolver import _http_get
        fetch = fetch or _http_get
        u = urlparse(src.url)
        api_host = (u.hostname or '').lower()
        if api_host not in self.download_hosts:
            raise AdapterError(f'unexpected_api_host:{api_host}')
        data = self._api(api_host, 'getpublinkdownload', u.query or '', fetch)
        hosts = [str(h).strip().lower() for h in (data.get('hosts') or []) if str(h).strip()]
        path = str(data.get('path') or '')
        if not hosts or not path.startswith('/'):
            raise AdapterError(f'bad_download_link:hosts={hosts[:2]} path={path[:30]!r}')
        # 主機由 API 回應決定，所以必須過我們自己的後綴檢查，不可照單全收
        for h in hosts:
            if h == 'pcloud.com' or h.endswith('.pcloud.com'):
                return SourceFile(name=src.name, url=f'https://{h}{path}', path=src.path,
                                  modified=src.modified, size=src.size,
                                  allowed_hosts=frozenset({h}),
                                  allowed_host_suffixes=self.download_host_suffixes)
        raise AdapterError(f'download_host_off_platform:{hosts[:3]}')
