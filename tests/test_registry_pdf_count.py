"""`permit_registry` 的 `pdf_count` 不可以跟著「名稱抽不出來」一起消失。

2026-10-14 對帳發現 41 個建照的來源明明有 PDF（`source_scan` 記 `reason: ok`，
抽查目標資料夾實際有 147／86／42 份），registry 的 `pdf_count` 卻連欄位都沒有。
根因是份數被放在「名字抽得出來」那個條件底下：

    if counts:                      # 名稱抽不出來就整筆不建
        names[permit] = {'name': ..., 'pdf_count': len(files)}
    ...
    if permit in drive_names:       # 於是份數也一起沒了
        entry['pdf_count'] = ...

而 `len(files)` 當場就在旁邊。下游（包括我自己的對帳）把「沒有欄位」讀成 0 份，
最後變成對外說「承造人的資料夾開著但裡面沒東西」——同一類錯誤的第三次
（#104 從日誌 grep 計數、#114 實測 0 與未取得混用、這次量到了卻沒寫下來）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geobingan_sync.steps import match_permits as mp


# ───────────────────────── 份數與名稱脫鉤 ─────────────────────────

class _Files:
    def __init__(self, pages, folders):
        self.pages = list(pages)
        self._folders = folders

    def list(self, **kwargs):
        page = self.pages.pop(0) if self.pages else {'files': []}

        class _R:
            def execute(_s):
                if isinstance(page, Exception):
                    raise page
                return page

        return _R()


class _Svc:
    def __init__(self, pages, folders=None):
        self._files = _Files(pages, folders or {})

    def files(self):
        return self._files


@pytest.fixture
def drive(monkeypatch):
    """把 `fetch_drive_pdf_names` 需要的三支函式換掉，只留 PDF 掃描那段。

    這三支是在函式**內**才 import 的，所以要換 `drive_utils` 上的名字；
    換 `match_permits` 的同名屬性是空操作（同 session 已踩過兩次）。
    """
    from geobingan_sync import drive_utils
    monkeypatch.setattr(drive_utils, 'list_top_level_folders',
                        lambda svc, drv: [{'id': 'F1', 'name': '110建字第0038號'}])
    monkeypatch.setattr(drive_utils, 'list_all_subfolders', lambda svc, drv: {})
    monkeypatch.setattr(drive_utils, 'build_folder_resolver',
                        lambda folders, subs: (lambda fid: None))
    return None


def _pdf(name):
    return {'name': name, 'mimeType': 'application/pdf', 'parents': ['F1']}


def test_pdf_count_survives_an_unextractable_name(drive):
    """檔名抽不出建案名稱，份數還是要寫下來。這就是那 41 案。"""
    svc = _Svc([{'files': [_pdf('1131223.pdf'), _pdf('20261014.pdf')]}])
    names, complete = mp.fetch_drive_pdf_names(svc)
    assert complete
    assert '110建字第0038號' in names, '有 PDF 的案一定要有一筆，名字抽不出來也要'
    assert names['110建字第0038號']['pdf_count'] == 2
    assert names['110建字第0038號']['name'] == '', '抽不到名字就留空，不要瞎猜'


def test_a_name_is_still_extracted_when_it_can_be(drive):
    """反面：抽得出名字時行為不變（投票制取最常出現的）。"""
    svc = _Svc([{'files': [_pdf('維昌臨沂監測報告1131223.pdf'),
                           _pdf('維昌臨沂監測報告1140101.pdf')]}])
    names, _ = mp.fetch_drive_pdf_names(svc)
    assert names['110建字第0038號']['name']
    assert names['110建字第0038號']['pdf_count'] == 2


def test_an_interrupted_scan_says_so(drive):
    """翻頁中斷時份數只是下限，函式要把「沒數完」講出來。"""
    svc = _Svc([{'files': [_pdf('a.pdf')], 'nextPageToken': 'p2'},
                RuntimeError('API 掛了')])
    names, complete = mp.fetch_drive_pdf_names(svc)
    assert complete is False, '中斷沒被講出來，呼叫端就會把下限當成確定份數'
    assert names['110建字第0038號']['pdf_count'] == 1, '回傳的是已數到的下限'


# ───────────────────────── 寫進 registry 的規則 ─────────────────────────

def test_complete_scan_writes_the_count():
    e = {}
    assert mp.apply_pdf_count(e, {'pdf_count': 146}, True) is True
    assert e['pdf_count'] == 146


def test_same_count_is_not_reported_as_a_change():
    e = {'pdf_count': 146}
    assert mp.apply_pdf_count(e, {'pdf_count': 146}, True) is False
    assert e['pdf_count'] == 146


def test_an_incomplete_scan_does_not_overwrite_the_count():
    """下限不可以蓋掉有定論的觀測（同 `apply_url_probe` 的規則）。

    沒有這一條，翻頁中斷那輪會把 146 份寫成 40 份，而且看起來完全正常——
    比「沒有數字」更糟，錯的數字長得像對的。
    """
    e = {'pdf_count': 146}
    assert mp.apply_pdf_count(e, {'pdf_count': 40}, False, now='2026-10-14T10:00:00') is True
    assert e['pdf_count'] == 146, '留著上一輪有定論的數字'
    assert e['pdf_count_scan_error_at'] == '2026-10-14T10:00:00', (
        '「這輪沒數完」本身要看得見，無聲降級等於沒發生')


def test_repeated_incomplete_scans_do_not_churn_the_entry():
    e = {'pdf_count': 146, 'pdf_count_scan_error_at': '2026-10-13T10:00:00'}
    assert mp.apply_pdf_count(e, {'pdf_count': 40}, False) is False
    assert e['pdf_count_scan_error_at'] == '2026-10-13T10:00:00', '保留第一次發現的時間'


def test_a_later_complete_scan_clears_the_marker():
    e = {'pdf_count': 146, 'pdf_count_scan_error_at': '2026-10-13T10:00:00'}
    assert mp.apply_pdf_count(e, {'pdf_count': 150}, True) is True
    assert e['pdf_count'] == 150
    assert 'pdf_count_scan_error_at' not in e, '數完了就要清掉，不然永遠掛著舊警訊'


def test_zero_is_written_when_the_scan_really_found_nothing():
    """掃完了、真的 0 份，那是實測 0，要寫下來（不是「沒量到」）。"""
    e = {'pdf_count': 5}
    assert mp.apply_pdf_count(e, {}, True) is True
    assert e['pdf_count'] == 0


# ───────────────────────── 名稱邏輯只看有名字的那一份 ─────────────────────────

def test_named_only_hides_the_nameless_entries():
    """份數用 `drive_names`、名稱用 `named_only()`。

    沒有這道分流，名稱合併那段會把 `name: ''` 當成「有名字」，於是既有名稱被
    覆寫成空字串——修一個欄位的漏寫，弄壞另一個欄位。
    """
    src = {'A': {'name': '維昌臨沂', 'pdf_count': 3},
           'B': {'name': '', 'pdf_count': 146},
           'C': {'pdf_count': 1}}
    named = mp.named_only(src)
    assert set(named) == {'A'}
    assert set(src) == {'A', 'B', 'C'}, '不可以改到來源（份數那邊還要用）'
