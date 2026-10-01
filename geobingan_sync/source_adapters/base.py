"""非 Drive 來源的共同型別與取檔邊界。

為什麼需要這一層（2026-09-29 實測）：建管處清單 440 案中 74 案不是 Drive 資料夾。
其中 27 案只是「Drive 連結藏在別的頁面裡」，link_resolver 已能救回。剩下 44 案是
真的放在別的空間，而現行同步只會 Drive→Drive 複製（`files().copy`），完全接不了。

這一層提供另一條路：**下載位元組 → 上傳進我們的目標資料夾**。adapter 只負責
「列出這個來源有哪些檔案、各自的直接下載網址」，取檔與安全檢查統一在這裡做，
不讓每個 adapter 各寫一份。
"""
import io
import os
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
    #: 取這個檔案時**每一跳**都必須落在這些主機上（含轉址），**精確比對**。
    #: adapter 負責填；空的代表沒人宣告過允許範圍，fetch_pdf_bytes 會拒絕（fail-closed）。
    allowed_hosts: FrozenSet[str] = frozenset()
    #: 允許的主機**後綴**（等於該網域本身、或其子網域）。只在平台用動態子網域發檔時
    #: 才宣告——pCloud 的取檔主機是 API 回應裡才知道的 `esgp1.pcloud.com`／
    #: `edef5.pcloud.com`，Dropbox 是 `uc<隨機>.dl.dropboxusercontent.com`，
    #: 兩者都無法事先列舉。刻意與 allowed_hosts **分開欄位**而不是讓精確清單也兼做
    #: 後綴：否則既有的 `public-be.redsun.tw` 會悄悄變成允許它所有子網域。
    allowed_host_suffixes: FrozenSet[str] = frozenset()


class SourceAdapter(Protocol):
    name: str

    def matches(self, url: str) -> bool: ...

    def list_files(self, url: str) -> List[SourceFile]: ...

    # 選用：取檔**之前**把 SourceFile 換成帶真實下載網址的版本。
    # 為什麼需要這個掛勾：有些平台列檔時拿不到下載網址，要再呼叫一次 API 換，
    # 而換來的網址是**短效**的（pCloud 實測約 1 小時到期）。在 list_files 就換會
    # 讓大批次的後段全部過期，所以必須緊貼下載那一刻才換。
    # 沒有這個方法的 adapter 走原路，list_files 給的 url 直接用。
    def resolve_download(self, src: SourceFile) -> SourceFile: ...


def _session():
    """沿用 link_resolver 的連線層守門：逐次連線檢查實際對端 IP、禁 proxy、
    不讀環境變數。來源 URL 來自政府公告 PDF（各承造人自行填寫）＝不可信輸入，
    取檔跟解析走同一套防護，不另開一條沒守門的路。"""
    from geobingan_sync.link_resolver import _guarded_session
    return _guarded_session()


def _assert_fetchable(url: str, allowed_hosts: FrozenSet[str],
                      allowed_host_suffixes: FrozenSet[str] = frozenset()) -> None:
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
    if host in allowed_hosts:
        return
    for suffix in allowed_host_suffixes or ():
        # 標籤邊界要對齊：`== suffix` 或 `.suffix` 結尾。只用 endswith(suffix) 會讓
        # `evil-pcloud.com` 通過；少了 `== suffix` 則該網域本身會被擋掉。
        if host == suffix or host.endswith('.' + suffix):
            return
    raise AdapterError(f'host_not_allowed:{host or "(none)"}')


MIN_SUFFIX_LABELS = 2          # 後綴至少要兩段（擋掉打成 'com'／'tw' 這種過寬的值）


def _check_suffixes(suffixes) -> frozenset:
    """後綴白名單的防呆。只擋明顯過寬的值；真正的把關仍是 code review。

    後綴只來自 adapter 的程式碼常數、不來自 payload，所以這裡不是安全邊界，是
    防打錯：`com` 或空字串會讓整個規則失效，而且不會有任何徵兆。
    """
    out = set()
    for raw in suffixes or ():
        sfx = str(raw).strip().lower().lstrip('.')
        if not sfx or len(sfx.split('.')) < MIN_SUFFIX_LABELS:
            raise AdapterError(f'suffix_too_broad:{raw!r}（至少需 {MIN_SUFFIX_LABELS} 段網域）')
        out.add(sfx)
    return frozenset(out)


def _open_guarded(url: str, hosts: frozenset, suffixes: frozenset, sess, max_hops: int):
    """逐跳跟轉址，每一跳在**送出請求之前**驗目的地，回最終的 streaming response。

    抽成共用函式而不是讓單檔與整包各寫一份：安全規則複製一次就會分叉，之後只有
    其中一條被修（#103 的告警真空正是這樣來的）。單檔與整包共用同一套
    scheme／埠號／主機白名單／跳數檢查。
    """
    from geobingan_sync.link_resolver import USER_AGENT
    current = url
    for _hop in range(max_hops):
        _assert_fetchable(current, hosts, suffixes)       # ← 發出請求之前
        r = sess.get(current, headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT,
                     stream=True, allow_redirects=False)
        location = r.headers.get('Location', '')
        if location:
            try:
                r.close()
            except Exception:                             # noqa: BLE001
                pass
            current = urljoin(current, location)
            continue
        if r.status_code != 200:
            raise AdapterError(f'http_{r.status_code}')
        return r
    raise AdapterError(f'too_many_hops:>{max_hops}')


def _prepare_scope(allowed_hosts, allowed_host_suffixes):
    """整理允許範圍並做 fail-closed 檢查。兩者皆空＝沒人宣告過＝不准連。"""
    hosts = frozenset(h.lower() for h in (allowed_hosts or ()))
    suffixes = _check_suffixes(allowed_host_suffixes)
    if not hosts and not suffixes:
        raise AdapterError('no_allowed_hosts')
    return hosts, suffixes


def fetch_pdf_bytes(url: str, allowed_hosts: FrozenSet[str], max_bytes: int = MAX_FILE_BYTES,
                    session_factory: Callable = None, max_hops: int = MAX_FETCH_HOPS,
                    allowed_host_suffixes: FrozenSet[str] = frozenset()) -> bytes:
    """下載並確認真的是 PDF，否則拋 AdapterError。

    三個刻意的選擇：

    1. **自己跟轉址，逐跳在送出請求前驗**（見 _open_guarded）。交給 requests 的
       ``allow_redirects=True`` 等於把主機邊界交還給對方伺服器。
    2. **超過大小上限就拋錯，不截斷。** 截斷會把半份壞掉的 PDF 上傳進監測報告
       資料夾，比缺一份還糟——後續解析拿到壞檔，錯誤會一路往下傳。
    3. **檢查 magic bytes，不信 Content-Type。** 對方回什麼標頭不由我們決定。
    """
    hosts, suffixes = _prepare_scope(allowed_hosts, allowed_host_suffixes)
    sess = (session_factory or _session)()
    try:
        r = _open_guarded(url, hosts, suffixes, sess, max_hops)
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
    except AdapterError:
        raise
    except Exception as e:                                # noqa: BLE001
        raise AdapterError(f'{type(e).__name__}:{e}') from e
    finally:
        try:
            sess.close()
        except Exception:                                 # noqa: BLE001
            pass


# ---------- 整包來源（bundle）----------
#
# 有些平台沒有便宜的列檔方式，只能一次下載整包。Dropbox 實測：檔案清單不在靜態
# HTML（連已知檔名都搜不到），zip 下載是 chunked、沒有 Content-Length／ETag／
# Last-Modified，所以**無法判斷有沒有新檔**。逆向其內部列檔 API 太脆（未文件化、
# 所需參數連 HTML 裡都沒有），申請官方 app 要多養一組憑證。
#
# 取整包的代價是頻寬，用「取用間隔」壓下來（見 adapter）。真正要小心的是解壓：
# 整包來自外部，必須擋路徑穿越與壓縮炸彈。

MAX_BUNDLE_BYTES = 512 * 1024 * 1024        # 整包下載上限；實測最大 284MB
MAX_BUNDLE_MEMBERS = 5000                   # 成員數上限
MAX_BUNDLE_UNCOMPRESSED = 2 * 1024 ** 3     # 解壓後總量上限（擋壓縮炸彈）


def stream_to_tempfile(url: str, allowed_hosts: FrozenSet[str],
                       allowed_host_suffixes: FrozenSet[str] = frozenset(),
                       max_bytes: int = MAX_BUNDLE_BYTES, session_factory: Callable = None,
                       max_hops: int = MAX_FETCH_HOPS) -> str:
    """把整包串流落到暫存檔，回檔案路徑。**呼叫端負責刪除。**

    落檔而不是進記憶體：實測最大一包 284MB，而單檔路徑的上限是 64MB，全讀進來
    既會被擋也不該這樣花記憶體。守門與單檔共用 _open_guarded，不另寫一份。

    超過上限拋錯並刪掉半成品——半個 zip 解壓會得到隨機數量的檔案，比完全拿不到更糟。
    """
    import tempfile
    hosts, suffixes = _prepare_scope(allowed_hosts, allowed_host_suffixes)
    sess = (session_factory or _session)()
    fd, path = tempfile.mkstemp(prefix='pdfsync_bundle_', suffix='.zip')
    total = 0
    try:
        r = _open_guarded(url, hosts, suffixes, sess, max_hops)
        with os.fdopen(fd, 'wb') as f:
            for c in r.iter_content(1024 * 1024):
                total += len(c)
                if total > max_bytes:
                    raise AdapterError(f'bundle_too_large:>{max_bytes}')
                f.write(c)
        if total == 0:
            raise AdapterError('bundle_empty')
        return path
    except AdapterError:
        _unlink(path)
        raise
    except Exception as e:                                    # noqa: BLE001
        _unlink(path)
        raise AdapterError(f'{type(e).__name__}:{e}') from e
    finally:
        try:
            sess.close()
        except Exception:                                     # noqa: BLE001
            pass


def _unlink(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def _safe_member_name(name: str):
    """回安全的 (子資料夾路徑, 檔名)，不安全則回 None。

    擋 zip slip：壓縮檔裡的路徑由對方控制，`../../x.pdf` 或絕對路徑會寫到資料夾外。
    我們其實不用這些路徑去寫檔（解出來的位元組直接上傳），但 path 會變成目標 Drive
    的子資料夾名稱，照樣不能讓它跳出去。
    """
    n = (name or '').replace('\\', '/')
    if not n or n.endswith('/'):
        return None                                           # 目錄項
    if n.startswith('/') or ':' in n.split('/')[0]:
        return None                                           # 絕對路徑／磁碟代號
    parts = [p for p in n.split('/') if p not in ('', '.')]
    if any(p == '..' for p in parts):
        return None
    if not parts:
        return None
    return '/'.join(parts[:-1]), parts[-1]


def iter_zip_pdfs(path: str, max_members: int = MAX_BUNDLE_MEMBERS,
                  max_uncompressed: int = MAX_BUNDLE_UNCOMPRESSED):
    """逐一吐出 zip 裡的 PDF：(子資料夾, 檔名, 位元組)。

    只收 `.pdf`、擋路徑穿越、擋壓縮炸彈（成員數與解壓總量上限），每個成員也要通過
    PDF magic bytes 檢查——壓縮檔裡的副檔名同樣是對方說了算。
    """
    import zipfile
    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:                                    # noqa: BLE001
        raise AdapterError(f'bad_zip:{type(e).__name__}') from e
    with zf:
        names = zf.namelist()
        if len(names) > max_members:
            raise AdapterError(f'too_many_members:{len(names)}>{max_members}')
        declared = sum(i.file_size for i in zf.infolist())
        if declared > max_uncompressed:
            raise AdapterError(f'uncompressed_too_large:{declared}>{max_uncompressed}')
        for name in names:
            safe = _safe_member_name(name)
            if safe is None:
                continue
            folder, fname = safe
            if not fname.lower().endswith('.pdf'):
                continue
            try:
                with zf.open(name) as fh:
                    data = fh.read()
            except Exception as e:                            # noqa: BLE001
                # 單一成員壞掉（CRC 不符、目錄被動過）不該讓整包的其他 200 份一起沒了。
                # 注意：宣告值造假正是由 zipfile 的 CRC 檢查在這裡擋下，所以不另寫一道
                # 「實際讀取量」上限——那道在宣告量已過關的前提下**永遠觸發不到**，
                # 付不出獨有的失敗序列（注回驗證時發現的）。
                print(f'  ⚠️ 整包成員無法讀取，略過 {fname}: {type(e).__name__}')
                continue
            if not data.startswith(PDF_MAGIC):
                continue                                      # 副檔名是 .pdf 但內容不是
            yield folder, fname, data
