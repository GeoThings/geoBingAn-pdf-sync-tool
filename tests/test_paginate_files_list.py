"""Tests for drive_utils.paginate_files_list 與 #67 修復。

背景（#67，同 #65 bug class）：files().list 單次上限 1000、未翻頁的呼叫
會靜默截斷。本檔驗共用 helper 的翻頁/重試語意，以及兩個改用 helper 的
call site（sync_permits.list_files_recursive、upload_pdfs fallback）翻頁到底。
"""
import sys
import os

import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from drive_utils import paginate_files_list


class _FakeResp:
    def __init__(self, status=500, reason='err'):
        self.status = status
        self.reason = reason


class _FakeFilesApi:
    """script 元素：dict（回應）或 HttpError（raise 一次後繼續吃下一個）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)

        class _Req:
            def __init__(self, it):
                self._it = it

            def execute(self):
                if isinstance(self._it, Exception):
                    raise self._it
                return self._it

        return _Req(item)


class _FakeService:
    def __init__(self, script):
        self._files = _FakeFilesApi(script)

    def files(self):
        return self._files


def test_follows_pagination_to_the_end():
    svc = _FakeService([
        {'files': [{'id': 'a'}], 'nextPageToken': 't2'},
        {'files': [{'id': 'b'}], 'nextPageToken': 't3'},
        {'files': [{'id': 'c'}]},
    ])
    items = paginate_files_list(svc, q='x', fields='files(id)')
    assert [i['id'] for i in items] == ['a', 'b', 'c']
    assert svc._files.calls[1]['pageToken'] == 't2'
    assert svc._files.calls[2]['pageToken'] == 't3'


def test_fields_auto_includes_next_page_token():
    svc = _FakeService([{'files': []}])
    paginate_files_list(svc, q='x', fields='files(id, name)')
    assert svc._files.calls[0]['fields'].startswith('nextPageToken')


def test_retries_on_429_then_succeeds():
    slept = []
    svc = _FakeService([
        HttpError(_FakeResp(429), b'rate'),
        {'files': [{'id': 'a'}]},
    ])
    items = paginate_files_list(svc, q='x', fields='files(id)',
                                sleep=slept.append)
    assert [i['id'] for i in items] == ['a']
    assert len(slept) == 1


def test_non_retryable_error_raises_immediately():
    svc = _FakeService([HttpError(_FakeResp(404), b'nf')])
    with pytest.raises(HttpError):
        paginate_files_list(svc, q='x', fields='files(id)', sleep=lambda s: None)
    assert len(svc._files.calls) == 1


def test_retries_exhausted_raises():
    errs = [HttpError(_FakeResp(503), b'x') for _ in range(4)]
    svc = _FakeService(errs)
    with pytest.raises(HttpError):
        paginate_files_list(svc, q='x', fields='files(id)',
                            retries=3, sleep=lambda s: None)
    assert len(svc._files.calls) == 4  # 1 + 3 retries


def test_top_level_folders_paginate_and_subfolders_fail_closed():
    """drive_utils 兩個舊迴圈收斂到 helper：翻頁到底 + 持久失敗 raise。"""
    from drive_utils import list_top_level_folders, list_all_subfolders

    svc = _FakeService([
        {'files': [{'id': 'f1', 'name': 'A'}], 'nextPageToken': 't2'},
        {'files': [{'id': 'f2', 'name': 'B'}]},
    ])
    folders = list_top_level_folders(svc, 'drv')
    assert [f['id'] for f in folders] == ['f1', 'f2']

    bad = _FakeService([HttpError(_FakeResp(403), b'denied')])
    with pytest.raises(HttpError):
        list_all_subfolders(bad, 'drv')


def test_sync_list_files_recursive_paginates(monkeypatch):
    """#67 本體：來源資料夾兩頁檔案 + 子資料夾遞迴，全數收齊。"""
    import sync_permits
    from sync_permits import PermitSync

    sync = PermitSync(city={'name': 'T', 'pdf_list_url': 'https://example.test/x.pdf'})
    svc = _FakeService([
        # root 第 1 頁：1 PDF + 1 子資料夾
        {'files': [
            {'id': 'p1', 'name': 'a.pdf', 'mimeType': 'application/pdf'},
            {'id': 'sub', 'name': 'S', 'mimeType': 'application/vnd.google-apps.folder'},
        ], 'nextPageToken': 't2'},
        # helper 先收齊 root 全部頁面才迭代，故第 2 個呼叫是 root 第 2 頁
        {'files': [{'id': 'p2', 'name': 'b.pdf', 'mimeType': 'application/pdf'}]},
        # 子資料夾單頁
        {'files': [{'id': 'p3', 'name': 'c.pdf', 'mimeType': 'application/pdf'}]},
    ])
    monkeypatch.setattr(sync, '_get_svc', lambda: svc)

    files = sync.list_files_recursive('root')

    names = sorted(f[1] for f in files)
    assert names == ['a.pdf', 'b.pdf', 'c.pdf']


def test_upload_fallback_paginates_per_folder(capsys):
    """upload fallback：批次掃描炸掉後，單一資料夾兩頁 PDF 不得截斷。"""
    import upload_pdfs
    from upload_pdfs import list_all_pdfs_with_folder_info

    folders = [{'id': 'f1', 'name': '110建字第0001號'}]
    svc = _FakeService([
        HttpError(_FakeResp(403), b'batch boom'),   # 批次第 1 頁失敗 → fallback
        {'files': [{'id': 'p1', 'name': 'a.pdf', 'modifiedTime': 'x'}],
         'nextPageToken': 't2'},
        {'files': [{'id': 'p2', 'name': 'b.pdf', 'modifiedTime': 'x'}]},
    ])

    result = list_all_pdfs_with_folder_info(svc, folders)

    assert sorted(p['name'] for p in result) == ['a.pdf', 'b.pdf']
    assert all(p['folder_name'] == '110建字第0001號' for p in result)


def test_upload_fallback_fails_closed_on_persistent_folder_error(capsys):
    """fallback 中任一資料夾持久失敗 → 整次掃描 raise，
    不得把其餘資料夾的部分結果當完整掃描回傳（review P2）。"""
    from upload_pdfs import list_all_pdfs_with_folder_info

    folders = [
        {'id': 'bad', 'name': '110建字第0001號'},
        {'id': 'good', 'name': '110建字第0002號'},
    ]
    svc = _FakeService([
        HttpError(_FakeResp(403), b'batch boom'),   # 批次失敗 → fallback
        HttpError(_FakeResp(403), b'bad folder'),   # bad 資料夾：非重試錯誤
        {'files': [{'id': 'p1', 'name': 'ok.pdf', 'modifiedTime': 'x'}]},  # good 資料夾
    ])

    with pytest.raises(RuntimeError, match='110建字第0001號'):
        list_all_pdfs_with_folder_info(svc, folders)

    out = capsys.readouterr().out
    assert '資料夾掃描失敗' in out
    # good 資料夾仍被掃過（先收集完整失敗清單再 raise）
    assert len(svc._files.calls) == 3
