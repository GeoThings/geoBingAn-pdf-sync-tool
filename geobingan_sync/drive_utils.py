"""
共用 Google Drive API 工具函數

提供 Shared Drive 資料夾掃描、子資料夾層級解析等共用操作，
避免在 sync_permits / match_permits / generate_permit_tracking_report 中重複實作。
"""
import time

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# files().list 單次呼叫上限 1000；未翻頁的呼叫曾造成靜默截斷資料漏失
# （#65：758 個資料夾內 PDF 從未上傳；#67：同步端來源資料夾 >1000 檔截斷）。
# 新的 files().list 呼叫一律走 paginate_files_list，不要手寫翻頁迴圈。
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def paginate_files_list(service, *, retries: int = 3, retry_base_delay: float = 2.0,
                        sleep=time.sleep, **list_kwargs) -> list:
    """完整翻頁的 files().list：跟隨 nextPageToken 到底，回傳所有 files。

    - `fields` 若未包含 nextPageToken 會自動補上（否則翻頁靜默失效）。
    - 每頁對 429/5xx 指數退避重試（retries 次）；其他錯誤或重試耗盡直接 raise，
      由呼叫端決定 fail-closed 行為——寧可炸也不回傳靜默截斷的部分結果。
    """
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
            except HttpError as e:
                status = getattr(getattr(e, 'resp', None), 'status', None)
                if status in _RETRYABLE_STATUS and attempt < retries:
                    delay = retry_base_delay * (2 ** attempt)
                    attempt += 1
                    print(f"  ⚠️ files().list {status}，第 {attempt}/{retries} 次重試"
                          f"（等待 {delay:.0f} 秒）...")
                    sleep(delay)
                    continue
                raise
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
