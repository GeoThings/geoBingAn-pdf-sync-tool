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
from typing import Callable, List, Optional, Protocol

MAX_FILE_BYTES = 64 * 1024 * 1024     # 單檔上限；報表實測 0.15–0.5MB，這是防呆不是門檻
MAX_REDIRECTS = 5
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


def fetch_pdf_bytes(url: str, max_bytes: int = MAX_FILE_BYTES,
                    session_factory: Callable = None) -> bytes:
    """下載並確認真的是 PDF，否則拋 AdapterError。

    兩個刻意的選擇：

    1. **超過上限就拋錯，不截斷。** 截斷會把半份壞掉的 PDF 上傳進監測報告資料夾，
       比缺一份還糟——後續解析拿到壞檔，錯誤會一路往下傳。
    2. **檢查 magic bytes，不信 Content-Type。** 對方伺服器回什麼標頭不由我們決定；
       真正決定我們要不要把它放進資料夾的，是內容本身。
    """
    from geobingan_sync.link_resolver import USER_AGENT
    sess = (session_factory or _session)()
    try:
        r = sess.get(url, headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT,
                     stream=True, allow_redirects=True)
        # 轉址由守門連線類別逐跳檢查對端 IP，這裡只限制跳數。
        if len(r.history) > MAX_REDIRECTS:
            raise AdapterError(f'too_many_redirects:{len(r.history)}')
        if r.status_code != 200:
            raise AdapterError(f'http_{r.status_code}')
        chunks, total = [], 0
        for c in r.iter_content(64 * 1024):
            total += len(c)
            if total > max_bytes:
                raise AdapterError(f'too_large:>{max_bytes}')
            chunks.append(c)
        data = b''.join(chunks)
    except AdapterError:
        raise
    except Exception as e:                                  # noqa: BLE001
        raise AdapterError(f'{type(e).__name__}:{e}') from e
    finally:
        try:
            sess.close()
        except Exception:                                   # noqa: BLE001
            pass
    if not data.startswith(PDF_MAGIC):
        raise AdapterError(f'not_pdf:{data[:8]!r}')
    return data
