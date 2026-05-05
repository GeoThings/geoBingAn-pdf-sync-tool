#!/usr/bin/env python3
"""清理 permit_registry.json 中失效的 Drive 資料夾連結。

每週 sync 中累積出現「⚠️ 列出檔案失敗 / Drive 資料夾讀取失敗 ... 404 File not found」
的工地，多是 folder 被刪除或搬移。registry 內的 source_url 仍指著舊 ID，
造成 log 噪音不斷增長。

用法：
    python3 cleanup_stale_folders.py             # dry-run，只列出失效項目
    python3 cleanup_stale_folders.py --apply     # 實際清掉 source_url，加 source_url_removed_at
    python3 cleanup_stale_folders.py --limit 50  # 只檢前 50 筆 (測試)
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REGISTRY_FILE = Path(__file__).parent / 'state' / 'permit_registry.json'
DRIVE_FOLDER_ID_RE = re.compile(r'/folders/([a-zA-Z0-9_-]+)')


def extract_folder_id(url: str):
    if not url:
        return None
    m = DRIVE_FOLDER_ID_RE.search(url)
    return m.group(1) if m else None


def is_folder_alive(service, folder_id: str) -> bool:
    """True = 存在且可讀；False = 404；其他錯誤 raise 上拋。"""
    from googleapiclient.errors import HttpError
    try:
        service.files().get(
            fileId=folder_id,
            fields='id',
            supportsAllDrives=True,
        ).execute()
        return True
    except HttpError as e:
        if e.resp.status == 404:
            return False
        raise


def main():
    parser = argparse.ArgumentParser(description='清理 permit_registry.json 中失效的 Drive 資料夾連結')
    parser.add_argument('--apply', action='store_true', help='實際寫回 (預設 dry-run)')
    parser.add_argument('--limit', type=int, default=0, help='只檢查前 N 筆 (測試用，0 = 全部)')
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).parent))
    from sync_permits import get_drive_service

    with open(REGISTRY_FILE, 'r', encoding='utf-8') as f:
        registry = json.load(f)

    candidates = [(k, v) for k, v in registry.items() if extract_folder_id(v.get('source_url'))]
    print(f'📊 registry 總 entry: {len(registry)}，含 Drive folder URL: {len(candidates)}')

    if args.limit > 0:
        candidates = candidates[:args.limit]
        print(f'   --limit 套用，只檢前 {len(candidates)} 筆')

    print(f'\n🔍 開始檢查 Drive folder 是否仍存在...')
    service = get_drive_service()

    dead = []
    errors = []
    for i, (permit, info) in enumerate(candidates, 1):
        folder_id = extract_folder_id(info['source_url'])
        try:
            if not is_folder_alive(service, folder_id):
                dead.append((permit, folder_id, info['source_url']))
        except Exception as e:
            errors.append((permit, folder_id, str(e)))
        if i % 50 == 0 or i == len(candidates):
            print(f'   進度 {i}/{len(candidates)}（失效 {len(dead)}，錯誤 {len(errors)}）')

    print()
    if errors:
        print(f'⚠️  {len(errors)} 筆檢查時發生非 404 錯誤（保留不動）：')
        for permit, fid, msg in errors[:5]:
            print(f'   {permit}: {msg[:120]}')
        if len(errors) > 5:
            print(f'   ... 及其他 {len(errors) - 5}')
        print()

    print(f'❌ 失效（404）共 {len(dead)} 筆')
    if dead:
        for permit, fid, url in dead:
            print(f'   {permit}  folder_id={fid}')

    if not dead:
        print('✅ 沒有需要清理的')
        return

    if args.apply:
        timestamp = datetime.now().isoformat(timespec='seconds')
        for permit, fid, url in dead:
            entry = registry[permit]
            entry['source_url_removed'] = url
            entry['source_url_removed_at'] = timestamp
            entry.pop('source_url', None)
        with open(REGISTRY_FILE, 'w', encoding='utf-8') as f:
            json.dump(registry, f, indent=2, ensure_ascii=False)
        print(f'\n✅ 已清掉 {len(dead)} 筆 source_url，原值保留在 source_url_removed（audit 用）')
    else:
        print(f'\n(dry-run，未寫回。確認清單後加 --apply 套用)')


if __name__ == '__main__':
    main()
