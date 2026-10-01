"""Dropbox 公開資料夾 adapter（整包下載）。

為什麼走整包而不是列檔（2026-10-01 實測）：

- 檔案清單**不在靜態 HTML**，連已知檔名都搜不到，是 JS 載入的
- zip 下載是 ``Transfer-Encoding: chunked``，**沒有** Content-Length／ETag／
  Last-Modified，所以無法判斷有沒有新檔
- 逆向其內部列檔 API 太脆（未文件化，所需的 link_key／secure_hash 連 HTML 裡都沒有）
- 申請官方 app 可拿到便宜的列檔，但要多養一組憑證，而這個專案每七天換 token 已經夠煩

所以只能 ``?dl=1`` 一次拿整包。實測 3 個案子分別 284MB／10.6MB／17.0MB，
合計 269 份 PDF。

**代價是頻寬，用取用間隔壓下來。** 監測報告是週報節奏，不需要每天抓；預設 7 天
一次，資料最多落後 7 天，剛好對上報告本身的產出週期。間隔狀態持久化，沒有時間戳
的（第一次）立刻抓一次把基準補上。

取檔主機是 ``uc<隨機>.dl.dropboxusercontent.com``，事先無法列舉，所以宣告的是
**後綴**（見 base 的 allowed_host_suffixes）。
"""
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta
from typing import Iterator, List, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from .base import (AdapterError, SourceFile, MAX_BUNDLE_BYTES, iter_zip_pdfs,
                   stream_to_tempfile, _unlink)

#: 已驗證的公開連結主機。
_HOSTS = frozenset({'www.dropbox.com', 'dropbox.com'})
#: 取檔主機後綴：Dropbox 用隨機子網域發檔，無法事先列舉。
DOWNLOAD_HOST_SUFFIXES = frozenset({'dl.dropboxusercontent.com'})

#: 整包取用間隔。監測報告是週報節奏，每天抓整包只是浪費頻寬。
FETCH_INTERVAL_DAYS = 7
STATE_FILE = './state/bundle_fetches.json'

_PATH_RE = re.compile(r'^/(scl/fo|sh)/[A-Za-z0-9_\-]+/')


def _parse(url: str):
    """回正規化後的「下載整包」網址，或 None。"""
    try:
        u = urlparse(url)
    except Exception:                                         # noqa: BLE001
        return None
    if u.scheme not in ('http', 'https') or u.port:
        return None
    if (u.hostname or '').lower() not in _HOSTS:
        return None
    if not _PATH_RE.match(u.path or ''):
        return None
    # dl=1 才會給整包 zip；原連結多半是 dl=0，要換掉而不是附加
    q = parse_qs(u.query or '', keep_blank_values=True)
    q['dl'] = ['1']
    return urlunparse(('https', 'www.dropbox.com', u.path, '',
                       urlencode({k: v[0] for k, v in q.items()}), ''))


def _load_state(path: str = None) -> dict:
    try:
        with open(path or STATE_FILE, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


#: 整包狀態的讀改寫要互斥。建案是並行處理的（ThreadPoolExecutor），而三個 Dropbox
#: 案子都寫同一個檔；無鎖的 read-modify-write 會互相覆寫。實測 24 個並行寫入只剩 4 個。
_STATE_LOCK = threading.Lock()


def _save_state(data: dict, path: str = None) -> None:
    """原子寫入。暫存檔名必須**每次唯一**——只帶 PID 的話同行程的執行緒會共用同一個
    暫存路徑，先完成的那個 os.replace 會把它搬走，後到的就 FileNotFoundError（實測過）。
    """
    p = path or STATE_FILE
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(p) + '.', suffix='.tmp', dir=d or '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except BaseException:
        _unlink(tmp)
        raise


def is_due(key: str, now: datetime = None, state_path: str = None,
           interval_days: int = FETCH_INTERVAL_DAYS) -> bool:
    """這一包該不該抓。沒有時間戳（第一次）一律抓，把基準補上。

    時間戳解析失敗也抓：fail-open 倒向「抓」這一側，髒資料不可以讓某一包永遠不更新
    （同 #100 失效資料夾重驗的紀律）。
    """
    now = now or datetime.now()
    raw = (_load_state(state_path).get(key) or {}).get('fetched_at')
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return True
    if last.tzinfo is not None:
        last = last.astimezone().replace(tzinfo=None)
    return (now - last) >= timedelta(days=interval_days)


def mark_fetched(key: str, pdf_count: int, now: datetime = None,
                 state_path: str = None) -> None:
    """記下這一包抓過了。**整段讀改寫都在鎖內**，否則並行的建案會互相覆寫時間戳，
    結果是大包被反覆下載（實測 24 個並行寫入只剩 4 個）。"""
    with _STATE_LOCK:
        st = _load_state(state_path)
        st[key] = {'fetched_at': (now or datetime.now()).isoformat(),
                   'pdf_count': int(pdf_count)}
        _save_state(st, state_path)


class DropboxAdapter:
    name = 'dropbox'
    download_hosts = frozenset({'www.dropbox.com'})
    download_host_suffixes = DOWNLOAD_HOST_SUFFIXES

    def matches(self, url: str) -> bool:
        return _parse(url) is not None

    def list_files(self, url: str) -> List[SourceFile]:
        """整包來源沒有便宜的列檔。走 fetch_all，不走 list_files。"""
        raise AdapterError('dropbox_is_a_bundle_source:use_fetch_all')

    def fetch_all(self, url: str, now: datetime = None, state_path: str = None,
                  interval_days: int = FETCH_INTERVAL_DAYS,
                  stream=None, iter_pdfs=None) -> Iterator[Tuple[SourceFile, bytes]]:
        """下載整包、解壓、逐一吐出 (SourceFile, 位元組)。

        未到取用間隔就什麼都不吐（並說明原因）——那不是錯誤，是刻意省頻寬。
        暫存檔在結束時一定刪掉，成功失敗都一樣。
        """
        dl = _parse(url)
        if not dl:
            raise AdapterError(f'not_a_dropbox_folder:{url}')
        if not is_due(dl, now=now, state_path=state_path, interval_days=interval_days):
            print(f'    ⏭️ 整包未到取用間隔（每 {interval_days} 天一次），本輪跳過')
            return

        stream = stream or stream_to_tempfile
        iter_pdfs = iter_pdfs or iter_zip_pdfs
        path = stream(dl, self.download_hosts,
                      allowed_host_suffixes=self.download_host_suffixes,
                      max_bytes=MAX_BUNDLE_BYTES)
        count = 0
        completed = False
        try:
            for folder, fname, data in iter_pdfs(path):
                count += 1
                yield SourceFile(name=fname, url=dl, path=folder, size=len(data)), data
            completed = True
        finally:
            _unlink(path)
            # **只有真的跑完才標記**（review P1）。原本放在 finally 無條件標記，於是
            # 壞 zip、解壓上限錯誤、或消費端中途停止都會寫出成功時間戳，接下來七天
            # 不再重試——已實測壞 zip 拋錯後隔天 is_due 仍是 False。
            # 沒標記的代價只是下一輪重抓一次，方向是安全的。
            if completed:
                try:
                    mark_fetched(dl, count, now=now, state_path=state_path)
                except Exception as e:                        # noqa: BLE001
                    print(f'    ⚠️ 整包取用時間戳寫入失敗: {type(e).__name__}'
                          f'——下一輪會重抓整包')
