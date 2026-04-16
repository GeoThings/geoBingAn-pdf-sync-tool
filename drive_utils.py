"""
共用 Google Drive API 工具函數

提供 Shared Drive 資料夾掃描、子資料夾層級解析等共用操作，
避免在 sync_permits / match_permits / generate_permit_tracking_report 中重複實作。
"""
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


def create_drive_service(credentials_file: str, scopes: list = None):
    """建立 Google Drive API service instance"""
    scopes = scopes or ['https://www.googleapis.com/auth/drive']
    credentials = service_account.Credentials.from_service_account_file(
        credentials_file, scopes=scopes)
    return build('drive', 'v3', credentials=credentials)


def list_top_level_folders(service, shared_drive_id: str,
                           fields: str = 'nextPageToken, files(id, name)') -> list:
    """列出 Shared Drive 頂層資料夾（分頁處理）

    Returns:
        list of dicts, each containing the requested fields
    """
    folders = []
    page_token = None
    while True:
        try:
            results = service.files().list(
                q=f"'{shared_drive_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields=fields,
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora='drive',
                driveId=shared_drive_id
            ).execute()
            folders.extend(results.get('files', []))
            page_token = results.get('nextPageToken')
            if not page_token:
                break
        except HttpError as e:
            print(f"⚠️  掃描 Drive 頂層資料夾失敗: {e}")
            break
    return folders


def list_all_subfolders(service, shared_drive_id: str) -> dict:
    """掃描 Shared Drive 所有子資料夾，回傳 folder_id → parent_id 對應

    Returns:
        dict mapping folder_id to its parent folder_id
    """
    subfolders = {}
    page_token = None
    while True:
        try:
            results = service.files().list(
                q="mimeType='application/vnd.google-apps.folder' and trashed=false",
                corpora='drive',
                driveId=shared_drive_id,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields='nextPageToken, files(id, parents)',
                pageSize=1000,
                pageToken=page_token
            ).execute()
            for f in results.get('files', []):
                parents = f.get('parents', [])
                if parents:
                    subfolders[f['id']] = parents[0]
            page_token = results.get('nextPageToken')
            if not page_token:
                break
        except HttpError as e:
            print(f"⚠️  掃描子資料夾失敗: {e}")
            break
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
