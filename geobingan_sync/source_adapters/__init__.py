"""非 Drive 來源 adapter 的註冊表。

只放**實測過、公開可列檔**的平台。

2026-09-29 逐一探測建管處清單裡 44 個非 Drive 來源，結論是超過半數根本打不開：

    SharePoint        16   302 轉到 /_forms/default.aspx，要登入
    Synology gofile    9   QuickConnect 轉到 NAS 後顯示「使用 DSM 帳號存取」
    NAS 裸 IP          3   連線逾時／404
    Google 非資料夾     2   drive/my-drive 私人路徑，等於貼錯連結

那不是我們寫得出 adapter 的問題，是對方沒有真的公開——承造人交給建管處的連結
需要帳號才看得到，公開揭露形同虛設。這些要走對外溝通，不是寫程式。

確認公開可列檔而值得寫 adapter 的：redsun 3 案（已實作）、Dropbox 3 案、
pCloud 1 案、MEGA 1 案。新增 adapter 前先實測列檔，不要憑主機名推測。
"""
from .base import AdapterError, SourceFile, fetch_pdf_bytes
from .dropbox import DropboxAdapter
from .pcloud import PCloudAdapter
from .redsun import RedsunAdapter

ADAPTERS = (RedsunAdapter(), PCloudAdapter(), DropboxAdapter())

__all__ = ['ADAPTERS', 'AdapterError', 'SourceFile', 'fetch_pdf_bytes', 'find_adapter']


def find_adapter(url: str):
    """回傳能處理這個來源的 adapter，沒有就回 None。"""
    if not url or not isinstance(url, str):
        return None
    for a in ADAPTERS:
        try:
            if a.matches(url):
                return a
        except Exception:                                   # noqa: BLE001
            continue
    return None
