"""Tests for upload_pdfs 資料夾掃描完整性。

背景：Drive 資料夾數超過單次 API 上限 1000 後，未翻頁的列表被靜默截斷
（1758 → 1000），截斷資料夾內的 PDF 又被 folder lookup 靜默丟棄，
導致 15 個建案的報告從未上傳。此處驗：
1. list_all_folders 會跟隨 nextPageToken 翻頁到底
2. list_all_pdfs_with_folder_info 對 parent 對不到的 PDF 顯式告警（不再靜默）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import upload_pdfs
from upload_pdfs import list_all_folders, list_all_pdfs_with_folder_info


class _FakeFilesApi:
    """script: list of response dicts，每次 list().execute() 依序回傳一個。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        resp = self.script.pop(0)
        return _FakeRequest(resp)


class _FakeRequest:
    def __init__(self, resp):
        self._resp = resp

    def execute(self):
        return self._resp


class _FakeService:
    def __init__(self, script):
        self._files = _FakeFilesApi(script)

    def files(self):
        return self._files


def test_list_all_folders_follows_pagination():
    page1 = {
        'files': [{'id': f'f{i}', 'name': f'folder{i}'} for i in range(1000)],
        'nextPageToken': 'tok2',
    }
    page2 = {
        'files': [{'id': f'f{i}', 'name': f'folder{i}'} for i in range(1000, 1758)],
    }
    svc = _FakeService([page1, page2])

    folders = list_all_folders(svc)

    assert len(folders) == 1758
    assert folders[0]['id'] == 'f0'
    assert folders[-1]['id'] == 'f1757'
    # 第二次呼叫必須帶上 pageToken
    assert len(svc.files().calls) >= 2
    assert svc._files.calls[1]['pageToken'] == 'tok2'


def test_list_all_folders_single_page():
    svc = _FakeService([{'files': [{'id': 'a', 'name': 'A'}]}])
    folders = list_all_folders(svc)
    assert len(folders) == 1
    assert len(svc._files.calls) == 1


def test_unmatched_parent_pdfs_are_reported(capsys):
    folders = [{'id': 'known', 'name': '110建字第0001號'}]
    pdf_page = {
        'files': [
            {'id': 'p1', 'name': 'in-scope.pdf', 'parents': ['known'],
             'modifiedTime': '2026-07-28T00:00:00Z'},
            {'id': 'p2', 'name': 'orphan-1.pdf', 'parents': ['unknown'],
             'modifiedTime': '2026-07-28T00:00:00Z'},
            {'id': 'p3', 'name': 'orphan-2.pdf', 'parents': ['unknown'],
             'modifiedTime': '2026-07-28T00:00:00Z'},
        ],
    }
    svc = _FakeService([pdf_page])

    result = list_all_pdfs_with_folder_info(svc, folders, use_cache=False, state=None)

    assert [p['name'] for p in result] == ['in-scope.pdf']
    out = capsys.readouterr().out
    assert '2 個 PDF' in out
    assert 'orphan-1.pdf' in out


def test_all_parents_matched_no_warning(capsys):
    folders = [{'id': 'known', 'name': '110建字第0001號'}]
    pdf_page = {
        'files': [
            {'id': 'p1', 'name': 'in-scope.pdf', 'parents': ['known'],
             'modifiedTime': '2026-07-28T00:00:00Z'},
        ],
    }
    svc = _FakeService([pdf_page])

    result = list_all_pdfs_with_folder_info(svc, folders, use_cache=False, state=None)

    assert len(result) == 1
    assert 'parent 資料夾不在資料夾列表中' not in capsys.readouterr().out
