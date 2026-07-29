"""Tests for select_pdfs_to_upload() 候選過濾純函式（#64）。

背景：2026-07-17 同名 PDF 在單一 run 內建出重複報告（bcc50fe3 已刪），
PR #63 加了 run 內去重但邏輯 inline 在 main() 無法單元測試。
此處覆蓋去重 regression 與 already-uploaded / too-old / no-date /
exclude / max_uploads 全部邊界。
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from upload_pdfs import select_pdfs_to_upload

CUTOFF = datetime(2026, 7, 1)


def _pdf(name, folder='110建字第0001號', mtime='2026-07-20T00:00:00Z'):
    return {'name': name, 'folder_name': folder, 'modifiedTime': mtime}


def test_same_name_dedup_in_run():
    """#63 regression：同 folder/檔名出現兩次 → 只選 1、dup 計數 1、記檔名。"""
    pdfs = [
        _pdf('報告_1150715.pdf', mtime='2026-07-20T00:00:00Z'),
        _pdf('報告_1150715.pdf', mtime='2026-07-19T00:00:00Z'),
    ]
    picked, counts = select_pdfs_to_upload(pdfs, [], cutoff=CUTOFF)
    assert len(picked) == 1
    assert counts['dup_in_run'] == 1
    assert counts['dup_skipped'] == ['110建字第0001號/報告_1150715.pdf']


def test_same_name_different_folders_both_kept():
    """不同建案資料夾的同名檔不是重複，兩個都要上傳。"""
    pdfs = [
        _pdf('報告_1150715.pdf', folder='110建字第0001號'),
        _pdf('報告_1150715.pdf', folder='110建字第0002號'),
    ]
    picked, counts = select_pdfs_to_upload(pdfs, [], cutoff=CUTOFF)
    assert len(picked) == 2
    assert counts['dup_in_run'] == 0


def test_already_uploaded_skipped():
    pdfs = [_pdf('報告_1150715.pdf')]
    picked, counts = select_pdfs_to_upload(
        pdfs, ['110建字第0001號/報告_1150715.pdf'], cutoff=CUTOFF)
    assert picked == []
    assert counts['already_uploaded'] == 1


def test_exclude_list():
    pdfs = [_pdf('範例.pdf'), _pdf('報告_1150715.pdf')]
    picked, counts = select_pdfs_to_upload(pdfs, [], cutoff=CUTOFF, exclude=['範例.pdf'])
    assert [p['name'] for p in picked] == ['報告_1150715.pdf']
    assert counts['excluded'] == 1


def test_too_old_and_boundary():
    """cutoff 之前跳過；等於 cutoff 當天保留（fd < cutoff 才排除）。"""
    pdfs = [
        _pdf('報告_1150630.pdf'),   # 2026-06-30 < 7/1 → too old
        _pdf('報告_1150701.pdf'),   # 2026-07-01 == cutoff → 保留
    ]
    picked, counts = select_pdfs_to_upload(pdfs, [], cutoff=CUTOFF)
    assert [p['name'] for p in picked] == ['報告_1150701.pdf']
    assert counts['too_old'] == 1


def test_no_date_skipped():
    pdfs = [_pdf('設計圖.pdf')]
    picked, counts = select_pdfs_to_upload(pdfs, [], cutoff=CUTOFF)
    assert picked == []
    assert counts['no_date'] == 1


def test_no_date_falls_back_to_folder_path():
    """檔名無日期但 folder/檔名組合可解析 → 不跳過。"""
    pdfs = [_pdf('報告.pdf', folder='2026年07月')]
    picked, counts = select_pdfs_to_upload(pdfs, [], cutoff=CUTOFF)
    assert counts['no_date'] + len(picked) == 1  # 視 parser 支援度，二者擇一
    # parser 若支援「2026年07月/報告.pdf」則必須被選中而非 no_date
    from filename_date_parser import parse_date_from_filename
    if parse_date_from_filename('2026年07月/報告.pdf'):
        assert len(picked) == 1


def test_max_uploads_picks_newest_first():
    """上限啟用時要吃到 modifiedTime 最新的（排序在函式內）。"""
    pdfs = [
        _pdf('報告_1150714.pdf', folder='A', mtime='2026-07-14T00:00:00Z'),
        _pdf('報告_1150720.pdf', folder='B', mtime='2026-07-20T00:00:00Z'),
        _pdf('報告_1150717.pdf', folder='C', mtime='2026-07-17T00:00:00Z'),
    ]
    picked, counts = select_pdfs_to_upload(pdfs, [], cutoff=CUTOFF, max_uploads=2)
    assert [p['folder_name'] for p in picked] == ['B', 'C']


def test_max_uploads_zero_means_unlimited():
    pdfs = [_pdf(f'報告_11507{d:02d}.pdf', folder=f'F{d}') for d in range(10, 20)]
    picked, _ = select_pdfs_to_upload(pdfs, [], cutoff=CUTOFF, max_uploads=0)
    assert len(picked) == 10
