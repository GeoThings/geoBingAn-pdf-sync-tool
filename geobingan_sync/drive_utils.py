"""
共用 Google Drive API 工具函數

提供 Shared Drive 資料夾掃描、子資料夾層級解析等共用操作，
避免在 sync_permits / match_permits / generate_permit_tracking_report 中重複實作。
"""
import http.client
import socket
import ssl
import time

import httplib2
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# files().list 單次呼叫上限 1000；未翻頁的呼叫曾造成靜默截斷資料漏失
# （#65：758 個資料夾內 PDF 從未上傳；#67：同步端來源資料夾 >1000 檔截斷）。
# 新的 files().list 呼叫一律走 paginate_files_list，不要手寫翻頁迴圈。
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 傳輸層的暫時性故障。2026-10-17、10-18 兩輪正式環境各栽在一次：
#   TimeoutError: The read operation timed out      （10/17，list_all_subfolders）
#   ConnectionResetError: [Errno 54] ...            （10/18，手寫的 PDF 批次掃描）
# 這些**不是** HttpError，所以原本以 HttpError 為鍵的三道防線（本函式的重試、
# 步驟 3 批次掃描的 fallback 觸發、fallback 自己的逐案 catch）同時沒接到，
# 一次網路抖動就讓整輪沒有產出報告。
#
# 型別要對準**實際函式庫會丟出來的那一個**。原本只列 socket.gaierror 是想像中的
# DNS 例外：httplib2 會把它轉成 httplib2.ServerNotFoundError，而後者的 MRO 只到
# HttpLib2Error → Exception，不是 gaierror／OSError／HTTPException 的子類，所以
# 那條規則對真正的呼叫路徑完全沒作用（PR #124 review 指出，經真實 HttpRequest
# 路徑實測：只呼叫一次、沒有退避）。交叉對照 googleapiclient 自己的
# _retry_request，它也把 ServerNotFoundError 列為傳輸錯誤。
_TRANSIENT_NETWORK_ERRORS = (
    TimeoutError,                   # 含 socket.timeout（3.10 起同一個型別）
    ConnectionError,                # Reset／Aborted／Refused、BrokenPipe
    socket.gaierror,                # 直接走 socket 時的 DNS 失敗
    httplib2.ServerNotFoundError,   # Drive 走 httplib2，DNS 失敗長這個樣子
    ssl.SSLError,                   # 握手或讀取中斷（憑證驗證失敗除外，見下）
    http.client.HTTPException,      # IncompleteRead、RemoteDisconnected、BadStatusLine
)


# 「這通 Drive 呼叫失敗了」——HTTP 層或傳輸層都算。呼叫端要決定 fail-safe
# （例如退回逐資料夾掃描）時用這一個；要決定「值不值得重試」用
# is_transient_drive_error。兩者是不同的問題，所以分成兩條規則各有名字。
DRIVE_CALL_ERRORS = (HttpError,) + _TRANSIENT_NETWORK_ERRORS


def is_transient_drive_error(exc: BaseException) -> bool:
    """這個例外值不值得重試：區分「對方暫時不行」與「我們問錯了」。

    只有這一條規則，呼叫點不要各自判斷——先前 `except HttpError` 被複製到
    三個地方，於是漏接也漏接了三次。
    """
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, 'resp', None), 'status', None)
        return status in _RETRYABLE_STATUS
    if isinstance(exc, ssl.SSLCertVerificationError):
        return False            # 重試不會讓憑證變對，立刻失敗比退避三輪有用
    return isinstance(exc, _TRANSIENT_NETWORK_ERRORS)


def paginate_files_list(service, *, retries: int = 3, retry_base_delay: float = 2.0,
                        sleep=None, **list_kwargs) -> list:
    """完整翻頁的 files().list：跟隨 nextPageToken 到底，回傳所有 files。

    - `fields` 若未包含 nextPageToken 會自動補上（否則翻頁靜默失效）。
    - 每頁對「暫時性故障」指數退避重試（retries 次）：429/5xx，以及讀取逾時、
      連線被重設一類的傳輸中斷（判準見 is_transient_drive_error）。
    - 其他錯誤或重試耗盡直接 raise，由呼叫端決定 fail-closed 行為——
      寧可炸也不回傳靜默截斷的部分結果。
    - 重試預算**每頁一份**（維持原本語意）：一趟掃描要翻幾十頁，整趟共用一份
      預算會讓長掃描比短呼叫更容易失敗。同一頁內不同型別的暫時性失敗則共用
      同一份預算，不會「逾時三次再加五百三次」。
    """
    # 預設值在呼叫時才解析：寫成 `sleep=time.sleep` 會在 def 時綁定，
    # 測試就注入不進去，於是沒有傳 sleep 的呼叫端一測就真的睡 14 秒。
    wait = sleep if sleep is not None else time.sleep

    fields = list_kwargs.get('fields', '')
    if fields and 'nextPageToken' not in fields:
        list_kwargs['fields'] = f'nextPageToken, {fields}'

    items = []
    page_token = None
    while True:
        attempt = 0
        while True:
            try:
                results = service.files().list(
                    pageSize=1000, pageToken=page_token, **list_kwargs
                ).execute()
                break
            except Exception as e:
                if not (is_transient_drive_error(e) and attempt < retries):
                    raise
                delay = retry_base_delay * (2 ** attempt)
                attempt += 1
                status = getattr(getattr(e, 'resp', None), 'status', None)
                what = status if status is not None else type(e).__name__
                print(f"  ⚠️ files().list {what}，第 {attempt}/{retries} 次重試"
                      f"（等待 {delay:.0f} 秒）...")
                wait(delay)
        items.extend(results.get('files', []))
        page_token = results.get('nextPageToken')
        if not page_token:
            return items


def create_drive_service(credentials_file: str, scopes: list = None):
    """建立 Google Drive API service instance"""
    scopes = scopes or ['https://www.googleapis.com/auth/drive']
    credentials = service_account.Credentials.from_service_account_file(
        credentials_file, scopes=scopes)
    return build('drive', 'v3', credentials=credentials)


def list_top_level_folders(service, shared_drive_id: str,
                           fields: str = 'files(id, name)') -> list:
    """列出 Shared Drive 頂層資料夾（完整翻頁 + retry）

    持久失敗會 raise（fail-closed）——原版吞錯回傳部分結果，
    會讓下游拿殘缺的資料夾清單當完整資料用（#65 同型風險）。

    Returns:
        list of dicts, each containing the requested fields
    """
    return paginate_files_list(
        service,
        q=f"'{shared_drive_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields=fields,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora='drive',
        driveId=shared_drive_id
    )


def list_all_subfolders(service, shared_drive_id: str) -> dict:
    """掃描 Shared Drive 所有子資料夾，回傳 folder_id → parent_id 對應

    持久失敗會 raise（fail-closed），不回傳靜默殘缺的對應表。

    Returns:
        dict mapping folder_id to its parent folder_id
    """
    subfolders = {}
    for f in paginate_files_list(
        service,
        q="mimeType='application/vnd.google-apps.folder' and trashed=false",
        corpora='drive',
        driveId=shared_drive_id,
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        fields='files(id, parents)'
    ):
        parents = f.get('parents', [])
        if parents:
            subfolders[f['id']] = parents[0]
    return subfolders


def build_folder_resolver(top_folder_to_permit: dict, all_subfolders: dict, max_depth: int = 5):
    """建立遞迴解析函數：任何子資料夾 → 所屬頂層建案

    Args:
        top_folder_to_permit: dict mapping top-level folder_id → permit_no
        all_subfolders: dict mapping folder_id → parent_id
        max_depth: 遞迴深度上限

    Returns:
        function(folder_id) → permit_no or None
    """
    cache = {}

    def resolve(folder_id, depth=0):
        if folder_id in cache:
            return cache[folder_id]
        if folder_id in top_folder_to_permit:
            cache[folder_id] = top_folder_to_permit[folder_id]
            return cache[folder_id]
        if depth > max_depth or folder_id not in all_subfolders:
            return None
        result = resolve(all_subfolders[folder_id], depth + 1)
        if result:
            cache[folder_id] = result
        return result

    return resolve
