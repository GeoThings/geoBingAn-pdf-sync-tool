"""非 Drive 來源的共同型別與取檔邊界。

為什麼需要這一層（2026-09-29 實測）：建管處清單 440 案中 74 案不是 Drive 資料夾。
其中 27 案只是「Drive 連結藏在別的頁面裡」，link_resolver 已能救回。剩下 44 案是
真的放在別的空間，而現行同步只會 Drive→Drive 複製（`files().copy`），完全接不了。

這一層提供另一條路：**下載位元組 → 上傳進我們的目標資料夾**。adapter 只負責
「列出這個來源有哪些檔案、各自的直接下載網址」，取檔與安全檢查統一在這裡做，
不讓每個 adapter 各寫一份。
"""
import io
import re
from dataclasses import dataclass, field
from typing import Callable, FrozenSet, List, Optional, Protocol
from urllib.parse import urljoin, urlparse

MAX_FILE_BYTES = 64 * 1024 * 1024     # 單檔上限；報表實測 0.15–0.5MB，這是防呆不是門檻
MAX_FETCH_HOPS = 5                    # **總請求數**上限（含第一次），不是「再加 5 次轉址」
TIMEOUT = 60                          # 下載比讀頁面久，給足時間
PDF_MAGIC = b'%PDF-'


class AdapterError(Exception):
    """來源無法列檔或取檔。呼叫端要把它當成「這一案這次跳過」，不可中斷整輪同步。"""


@dataclass(frozen=True)
class SourceFile:
    """來源端的一個檔案。欄位刻意對齊 Drive 那條路的 (filename, path)，
    讓去重與子資料夾邏輯兩條路共用，不必為 adapter 另寫一套。"""
    name: str
    url: str
    path: str = ''          # 相對子資料夾；空字串＝放在建案資料夾根層
    modified: str = ''      # ISO 時間，可空
    size: int = 0
    #: 取這個檔案時**每一跳**都必須落在這些主機上（含轉址）。adapter 負責填；
    #: 空的代表沒人宣告過允許範圍，fetch_pdf_bytes 會直接拒絕（fail-closed）。
    allowed_hosts: FrozenSet[str] = frozenset()


class SourceAdapter(Protocol):
    name: str

    def matches(self, url: str) -> bool: ...

    def list_files(self, url: str) -> List[SourceFile]: ...


def _session():
    """沿用 link_resolver 的連線層守門：逐次連線檢查實際對端 IP、禁 proxy、
    不讀環境變數。來源 URL 來自政府公告 PDF（各承造人自行填寫）＝不可信輸入，
    取檔跟解析走同一套防護，不另開一條沒守門的路。"""
    from geobingan_sync.link_resolver import _guarded_session
    return _guarded_session()


def _assert_fetchable(url: str, allowed_hosts: FrozenSet[str]) -> None:
    """在**發出請求之前**檢查這一跳的目的地。

    只驗第一個網址是不夠的（review P1）：白名單主機只要回一個 302，就能把我們
    導去任意公網主機，而連線層守門只擋得掉內網 IP、擋不了「跑去別人家」。所以
    轉址必須自己逐跳跟，而且檢查要在送出請求前做——事後才發現，請求早就發出去了。
    """
    u = urlparse(url)
    if u.scheme not in ('http', 'https'):
        raise AdapterError(f'bad_scheme:{u.scheme or "(none)"}')
    if u.port is not None:
        # 白名單是主機名，不含埠號。允許改埠等於允許連到同名主機上的其他服務。
        raise AdapterError(f'port_not_allowed:{u.port}')
    host = (u.hostname or '').lower()
    if host not in allowed_hosts:
        raise AdapterError(f'host_not_allowed:{host or "(none)"}')


def fetch_pdf_bytes(url: str, allowed_hosts: FrozenSet[str], max_bytes: int = MAX_FILE_BYTES,
                    session_factory: Callable = None, max_hops: int = MAX_FETCH_HOPS) -> bytes:
    """下載並確認真的是 PDF，否則拋 AdapterError。

    三個刻意的選擇：

    1. **自己跟轉址，逐跳在送出請求前驗**（scheme／埠號／主機白名單／跳數）。
       交給 requests 的 ``allow_redirects=True`` 等於放棄主機邊界——第一個網址
       驗過了，之後對方想把我們導去哪都行。跳數上限也必須**在發請求前**檢查，
       事後計數時超額的那一台早就被敲過了。
    2. **超過大小上限就拋錯，不截斷。** 截斷會把半份壞掉的 PDF 上傳進監測報告
       資料夾，比缺一份還糟——後續解析拿到壞檔，錯誤會一路往下傳。
    3. **檢查 magic bytes，不信 Content-Type。** 對方伺服器回什麼標頭不由我們
       決定；真正決定我們要不要把它放進資料夾的，是內容本身。

    平台日後若改用 CDN，把 CDN 主機加進 adapter 的白名單，不要放寬這裡的規則。
    """
    from geobingan_sync.link_resolver import USER_AGENT
    hosts = frozenset(h.lower() for h in (allowed_hosts or ()))
    if not hosts:
        raise AdapterError('no_allowed_hosts')      # 沒人宣告允許範圍＝不准連

    sess = (session_factory or _session)()
    current = url
    try:
        for _hop in range(max_hops):
            _assert_fetchable(current, hosts)       # ← 發出請求之前
            r = sess.get(current, headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT,
                         stream=True, allow_redirects=False)
            location = r.headers.get('Location', '')
            if location:
                try:
                    r.close()
                except Exception:                   # noqa: BLE001
                    pass
                current = urljoin(current, location)
                continue
            if r.status_code != 200:
                raise AdapterError(f'http_{r.status_code}')
            chunks, total = [], 0
            for c in r.iter_content(64 * 1024):
                total += len(c)
                if total > max_bytes:
                    raise AdapterError(f'too_large:>{max_bytes}')
                chunks.append(c)
            data = b''.join(chunks)
            if not data.startswith(PDF_MAGIC):
                raise AdapterError(f'not_pdf:{data[:8]!r}')
            return data
        raise AdapterError(f'too_many_hops:>{max_hops}')
    except AdapterError:
        raise
    except Exception as e:                          # noqa: BLE001
        raise AdapterError(f'{type(e).__name__}:{e}') from e
    finally:
        try:
            sess.close()
        except Exception:                           # noqa: BLE001
            pass
