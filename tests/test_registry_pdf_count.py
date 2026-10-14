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


# ───────── production 接線：合併層要知道「本輪掃到哪些案」（#118 review P1） ─────────
#
# 上面那些純函式測試涵蓋了規則，卻**測不到呼叫條件**。合併層寫的是
# `if permit in drive_names:`，所以只要那個字典的鍵是「找到 PDF 的建照」，
# 既有 `pdf_count: 5` 的案在 0 份時根本進不到合併層，舊數字會永遠留著——
# 規則全綠、production 不會歸零。這一組測試走真正的 `build_registry()`。

def _build(monkeypatch, registry, scan, complete=True, gov=None):
    """跑真正的 build_registry()，只把外部服務換掉，回傳提交出去的 registry。

    `gov` 很重要：合併迴圈走的是 `set(gov_data) | set(drive_names)`，不是
    registry 的鍵。要測「不在掃描範圍的案不可以被動到」，那案就必須從政府清單
    進到迴圈裡——否則測試根本沒走到被防的判斷，無論怎麼寫都會過（這個洞是
    注入 #3 打出來的：把 `if permit in drive_names` 改成 `if True` 時，
    1126 條全綠）。
    """
    committed = {}

    class _Creds:
        @staticmethod
        def from_service_account_file(*a, **k):
            return object()

    class _SvcAccount:
        Credentials = _Creds

    monkeypatch.setattr(mp, 'service_account', _SvcAccount)
    monkeypatch.setattr(mp, 'build', lambda *a, **k: object())
    monkeypatch.setattr(mp, 'load_existing_registry', lambda: registry)
    monkeypatch.setattr(mp, 'fetch_gov_pdf_data', lambda city=None: gov or {})
    monkeypatch.setattr(mp, 'fetch_source_folder_names',
                        lambda gov, svc, prior_registry=None: ({}, {}, '2026-10-14T10:00:00'))
    monkeypatch.setattr(mp, 'fetch_drive_pdf_names', lambda svc: (scan, complete))
    monkeypatch.setattr(mp, 'fetch_api_projects', lambda: [])
    monkeypatch.setattr(mp, 'fetch_api_report_categories', lambda: {})
    monkeypatch.setattr(mp, 'fetch_live_alerts', lambda: {})
    monkeypatch.setattr(mp, '_write_url_404_csv', lambda reg: None)
    monkeypatch.setattr(mp, 'commit_registry_with_deaths',
                        lambda reg, prior, **k: committed.update(reg))
    mp.build_registry()
    return committed


def test_a_permit_that_lost_all_its_pdfs_is_zeroed(monkeypatch):
    """掃完了、這案 0 份 → registry 要歸零。

    這是 review 的 P1：舊寫法只回傳「找到 PDF 的建照」，所以這案進不到合併層，
    registry 會一直說它有 5 份。帳上多出 5 份報告，而且看起來完全正常。
    """
    reg = {'A建字第0001號': {'name': '維昌臨沂', 'pdf_count': 5}}
    out = _build(monkeypatch, reg,
                 {'A建字第0001號': {'name': '維昌臨沂', 'pdf_count': 0}})
    assert out['A建字第0001號']['pdf_count'] == 0, (
        '在掃描範圍內、掃完了是 0 份，就要寫 0——舊數字留著就是謊報')


def test_a_permit_outside_the_scan_scope_is_left_alone(monkeypatch):
    """反面：這案在共用雲端硬碟沒有資料夾 → 我們沒量過，不可以動它。

    沒有這一條，「歸零」會變成「把沒量過的全部清成 0」——另一個方向的同一種錯。
    這就是失效連結那 33 案的樣子：在政府清單上（所以會被迴圈走到），但我們這邊
    沒有對應資料夾（所以不在掃描範圍）。測資一定要讓它**真的進到迴圈**，否則這
    條測試量的是「迴圈沒走到它」，不是「守門有效」。
    """
    reg = {'B建字第0002號': {'name': '別人家', 'pdf_count': 5}}
    out = _build(monkeypatch, reg, {},
                 gov={'B建字第0002號': {'source_url': 'https://drive.google.com/drive/folders/X'}})
    assert out['B建字第0002號']['pdf_count'] == 5, '沒量過不可以歸零'
    assert 'pdf_count_scan_error_at' not in out['B建字第0002號'], (
        '沒量過也不該掛「這輪沒數完」的標記')


def test_an_interrupted_scan_marks_the_in_scope_permits(monkeypatch):
    """翻頁中斷：範圍內的案保留舊數字並留下標記，由 production 路徑驗。"""
    reg = {'A建字第0001號': {'name': '維昌臨沂', 'pdf_count': 146}}
    out = _build(monkeypatch, reg,
                 {'A建字第0001號': {'name': '維昌臨沂', 'pdf_count': 40}},
                 complete=False)
    e = out['A建字第0001號']
    assert e['pdf_count'] == 146, '下限不可以蓋掉有定論的數字'
    assert e.get('pdf_count_scan_error_at'), '「這輪沒數完」要留在資料裡'


def test_the_scan_scope_includes_folders_with_no_pdfs(drive, monkeypatch):
    """`fetch_drive_pdf_names` 的鍵是掃描範圍：有資料夾、0 份也要有一筆。

    這條和上面的接線測試是一對——合併層靠這個範圍才分得出 0 份與沒量過。
    """
    from geobingan_sync import drive_utils
    monkeypatch.setattr(drive_utils, 'list_top_level_folders',
                        lambda svc, drv: [{'id': 'F1', 'name': '110建字第0038號'},
                                          {'id': 'F2', 'name': '111建字第0091號'}])
    svc = _Svc([{'files': [_pdf('1131223.pdf')]}])
    names, complete = mp.fetch_drive_pdf_names(svc)
    assert complete
    assert names['111建字第0091號']['pdf_count'] == 0, (
        '資料夾在、一份 PDF 都沒有，就是實測 0——要進範圍才歸得了零')
    assert names['110建字第0038號']['pdf_count'] == 1
