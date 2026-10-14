"""失效 URL 列管表要分出「仍在市府清單」與「已離開清單」兩種 404。

2026-10-14 的教訓：那張表 47 筆被我讀成同一件事，對外講出了錯的案數。實際上
14 筆的死連結還掛在市府清單上（建管處那邊真的有東西要更新），另外 33 筆已經
不在清單上——同步的走訪範圍也來自那份清單，所以那 33 案連量測紀錄都不會有，
拿去請對方更新沒有意義。

舊版還有一個小問題：`檢查日` 每輪把每一列都寫成當天，於是「哪一批是什麼時候
確認失效的」被整張表抹平（檔案裡 47 筆全是 2026-10-14），而那是回頭對帳時唯一
能分辨批次的線索。
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geobingan_sync.steps import match_permits as mp

def _complete(permits):
    """完整抽到的清單成員。測試一律明寫完整性，不給預設值。"""
    return mp.ListMembership(frozenset(permits), True)


def _partial(permits, detail='37 頁中有 1 頁抽不到文字（第 12 頁）'):
    """只抽到部分頁面：集合是下限。"""
    return mp.ListMembership(frozenset(permits), False, detail)


_DEAD = {
    'A建字第0001號': {'name': '還在清單上', 'gov_pdf_url_status': '404',
                     'source_url': 'https://drive.google.com/drive/folders/FID_A',
                     'gov_pdf_url_checked_at': '2026-10-08T10:06:37.446286'},
    'B建字第0002號': {'name': '已離開清單', 'gov_pdf_url_status': '404',
                     'source_url': 'https://drive.google.com/drive/folders/FID_B'},
    'C建字第0003號': {'name': '活的', 'gov_pdf_url_status': 'alive',
                     'source_url': 'https://drive.google.com/drive/folders/FID_C'},
}


def test_the_table_separates_still_listed_from_gone_from_the_list():
    rows = mp.url_404_rows(_DEAD, _complete({'A建字第0001號', 'C建字第0003號'}))
    got = {r[0]: r[4] for r in rows}
    assert got == {'A建字第0001號': '是', 'B建字第0002號': '否'}, (
        '兩種 404 要分開；混在一起就會把「已離開清單」的案拿去請建管處更新')


def test_alive_permits_stay_out_of_the_table():
    rows = mp.url_404_rows(_DEAD, _complete({'A建字第0001號', 'C建字第0003號'}))
    assert 'C建字第0003號' not in {r[0] for r in rows}


def test_still_listed_rows_come_first():
    """這張表是給人看的，要處理的那幾列要在最前面。

    測資刻意讓「仍在清單」那案的字號排在最後（`Z…`）：第一版用 `_DEAD`，而它的
    字號順序剛好就是想要的順序，所以把排序整個拿掉也照樣綠——注入 #4 打出來的。
    """
    dead = {
        'A建字第0001號': dict(_DEAD['B建字第0002號']),     # 已離開清單
        'M建字第0500號': dict(_DEAD['B建字第0002號']),     # 已離開清單
        'Z建字第0999號': dict(_DEAD['A建字第0001號']),     # 仍在清單
    }
    rows = mp.url_404_rows(dead, _complete({'Z建字第0999號'}))
    assert [r[0] for r in rows] == ['Z建字第0999號', 'A建字第0001號', 'M建字第0500號'], (
        '仍在清單的要排最前面，其餘各組內再按字號')
    assert [r[4] for r in rows] == ['是', '否', '否']


def test_the_recheck_date_is_per_permit_not_the_output_date():
    rows = mp.url_404_rows(_DEAD, _complete({'A建字第0001號'}))
    by = {r[0]: r[5] for r in rows}
    assert by['A建字第0001號'] == '2026-10-08', (
        '要寫這一案自己的重驗日；寫成輸出當天就會把批次資訊整張抹平')
    assert by['B建字第0002號'] == '無紀錄', '沒有重驗過就要說沒有，不要填一個日期'


def test_an_unavailable_list_is_a_third_state_not_everyone_left():
    """清單整份沒取到時，不可以把每一列都講成「已離開」。

    這條測試的第一版把**錯的行為釘住了**：我斷言兩列都是「否」，還在 docstring
    裡替它找理由說「那在資料上是真的」。不是真的——空的 gov_permits 代表這輪
    沒有取到清單（上游 PDF 改版、解析器回歸都會長成零筆），那是量不到，不是
    量到 0。#119 review P1 指出來的，而這正是這張表分欄要防的同一件事。
    """
    rows = mp.url_404_rows(_DEAD, _complete(set()))
    assert [r[4] for r in rows] == [mp.LIST_UNKNOWN, mp.LIST_UNKNOWN], (
        '沒取到清單就只能記未取得；寫「否」等於對外宣稱這些案都離開清單了')


def test_the_writer_refuses_to_guess_when_the_list_is_missing():
    """守門：`gov_permits` 沒有預設值，漏傳要當場爆掉。

    有預設空集合的話，任何漏傳的呼叫點都會靜默把整張表判成「已離開」。
    """
    import pytest
    with pytest.raises(TypeError):
        mp._write_url_404_csv(_DEAD)


def test_the_csv_has_the_new_column_and_a_summary_line(tmp_path):
    out = tmp_path / 'gov_pdf_url_404.csv'
    mp._write_url_404_csv(_DEAD, _complete({'A建字第0001號'}), out_path=str(out))
    rows = list(csv.reader(out.read_text(encoding='utf-8-sig').splitlines()))
    assert rows[0][4] == '本輪仍在市府清單'
    assert rows[0][5] == '最後重驗日'
    assert rows[1][0] == 'A建字第0001號' and rows[1][4] == '是'
    assert rows[2][0] == 'B建字第0002號' and rows[2][4] == '否'
    tail = [c for r in rows[3:] for c in r if c]
    assert any('輸出日' in c for c in tail), '檔案產生日改成獨立一列，不混進逐案欄位'
    assert any('仍在清單 1 筆' in c for c in tail), '摘要要直接講兩種各幾筆'


def test_the_csv_says_the_list_was_unavailable(tmp_path):
    out = tmp_path / 'gov_pdf_url_404.csv'
    mp._write_url_404_csv(_DEAD, _complete(set()), out_path=str(out))
    text = out.read_text(encoding='utf-8-sig')
    rows = list(csv.reader(text.splitlines()))
    verdicts = {r[4] for r in rows[1:] if len(r) > 4}
    assert verdicts == {mp.LIST_UNKNOWN}, (
        f'沒取到清單時每一列都只能是未取得，實際 {verdicts}')
    assert '未取到市府清單' in text, '摘要要講清楚這輪沒取到清單'
    # 斷言放在判定欄，不是整份文字：測資的建案名稱本來就叫「已離開清單」，
    # 用整份 grep 會被自己的測資打中（第一版就是這樣紅的）。
    assert not any('已離開清單' in c for r in rows[-2:] for c in r), (
        '摘要列不可以在沒取到清單時宣告任何一案已離開')


def test_the_writer_passes_the_current_list_through(monkeypatch, tmp_path):
    """守門：build_registry 要把**本輪**的清單成員交給列管表。

    漏傳就等於整張表都是「否」，而那會把「建管處要更新的 14 筆」藏起來。
    """
    seen = {}
    monkeypatch.setattr(mp, '_write_url_404_csv',
                        lambda reg, membership, **k:
                        seen.update(reg=reg, gov=set(membership.permits)))

    class _Creds:
        @staticmethod
        def from_service_account_file(*a, **k):
            return object()

    class _SvcAccount:
        Credentials = _Creds

    monkeypatch.setattr(mp, 'service_account', _SvcAccount)
    monkeypatch.setattr(mp, 'build', lambda *a, **k: object())
    monkeypatch.setattr(mp, 'load_existing_registry', lambda: dict(_DEAD))
    monkeypatch.setattr(mp, 'fetch_gov_pdf_data',
                        lambda city=None: ({'A建字第0001號': {'source_url': 'u'}},
                                           _complete({'A建字第0001號', 'B建字第0002號'})))
    monkeypatch.setattr(mp, 'fetch_source_folder_names',
                        lambda gov, svc, prior_registry=None: ({}, {}, '2026-10-14T10:00:00'))
    monkeypatch.setattr(mp, 'fetch_drive_pdf_names', lambda svc: ({}, True))
    monkeypatch.setattr(mp, 'fetch_api_projects', lambda: [])
    monkeypatch.setattr(mp, 'fetch_api_report_categories', lambda: {})
    monkeypatch.setattr(mp, 'fetch_live_alerts', lambda: {})
    monkeypatch.setattr(mp, 'commit_registry_with_deaths', lambda reg, prior, **k: None)
    mp.build_registry()
    assert seen['gov'] == {'A建字第0001號', 'B建字第0002號'}, (
        f"列管表拿到的是 {seen.get('gov')}；要的是本輪**清單成員**，"
        '不是 gov_data 的鍵（後者少了沒解析出連結的案）')


# ───────── production 路徑：空解析不可以變成「全部離開清單」（#119 review P1） ─────────

def _run_build(monkeypatch, registry, gov, in_list, complete=True):
    """跑真正的 build_registry()，回傳列管表算出來的列。

    `gov` 是「解析出連結的案」、`in_list` 是「清單成員」，兩個都要明寫：
    第二輪 review 的 P1 就是這兩者被當成同一件事。
    """
    seen = {}
    monkeypatch.setattr(mp, '_write_url_404_csv',
                        lambda reg, membership, **k:
                        seen.update(rows=mp.url_404_rows(reg, membership)))

    class _Creds:
        @staticmethod
        def from_service_account_file(*a, **k):
            return object()

    class _SvcAccount:
        Credentials = _Creds

    monkeypatch.setattr(mp, 'service_account', _SvcAccount)
    monkeypatch.setattr(mp, 'build', lambda *a, **k: object())
    monkeypatch.setattr(mp, 'load_existing_registry', lambda: dict(registry))
    monkeypatch.setattr(mp, 'fetch_gov_pdf_data',
                        lambda city=None: (gov, _complete(in_list) if complete
                                          else _partial(in_list)))
    monkeypatch.setattr(mp, 'fetch_source_folder_names',
                        lambda g, svc, prior_registry=None: ({}, {}, '2026-10-14T10:00:00'))
    monkeypatch.setattr(mp, 'fetch_drive_pdf_names', lambda svc: ({}, True))
    monkeypatch.setattr(mp, 'fetch_api_projects', lambda: [])
    monkeypatch.setattr(mp, 'fetch_api_report_categories', lambda: {})
    monkeypatch.setattr(mp, 'fetch_live_alerts', lambda: {})
    monkeypatch.setattr(mp, 'commit_registry_with_deaths', lambda reg, prior, **k: None)
    mp.build_registry()
    return seen['rows']


def test_an_empty_parse_does_not_mark_the_whole_table_gone(monkeypatch):
    """上游 PDF 改版或解析器回歸會讓清單靜默變成零筆。那是量不到。

    走的是真正的 build_registry()，所以這條同時釘住「空集合有沒有一路傳到判定」。
    """
    rows = _run_build(monkeypatch, _DEAD, {}, set())
    assert {r[4] for r in rows} == {mp.LIST_UNKNOWN}, (
        '清單解析成零筆時，整張表只能是未取得——寫「否」就是對外宣稱全部離開')


def test_a_non_empty_list_still_marks_who_left(monkeypatch):
    """對照組：清單正常時，不在清單上的案仍然要被判成「已離開」。

    少了這條，把判定改成「永遠未取得」也會全綠——那是另一個方向的靜默。
    """
    rows = _run_build(monkeypatch, _DEAD,
                      {'A建字第0001號': {'source_url': 'u'}}, {'A建字第0001號'})
    got = {r[0]: r[4] for r in rows}
    assert got == {'A建字第0001號': '是', 'B建字第0002號': '否'}


def test_a_permit_in_the_list_without_a_link_is_not_marked_gone(monkeypatch):
    """清單上有 A、B，但只有 A 解析出連結——B 仍然在清單上。

    #119 review 第二輪的 P1：列管表原本拿 `gov_data` 的鍵當清單成員，而那只有
    「解析出連結」的案。上游把某案的網址換成圖片、或 regex 漏掉一種空間，該案
    就會被標成「已離開清單」，然後我們去查一個根本沒離開的案。歷史日誌裡這個
    落差一度是 439 案中的 71 案。
    """
    rows = _run_build(monkeypatch, _DEAD,
                      {'A建字第0001號': {'source_url': 'u'}},        # 只有 A 有連結
                      {'A建字第0001號', 'B建字第0002號'})            # 清單上兩案都有
    got = {r[0]: r[4] for r in rows}
    assert got == {'A建字第0001號': '是', 'B建字第0002號': '是'}, (
        f'實際 {got}：沒解析出連結 ≠ 已離開清單')


# ───────── 生產端：清單成員由 permits_in_list 來，不是解析結果 ─────────

def _fake_gov_pdf(monkeypatch, tmp_path, mapping, permits_in_list, complete=True):
    """讓 fetch_gov_pdf_data 跑真正的程式碼，只換掉 PDF 下載與解析。"""
    pdf = tmp_path / 'list.pdf'
    pdf.write_bytes(b'%PDF-1.4 fake')

    class _PS:
        def __init__(self, city=None):
            self.permits_in_list = list(permits_in_list)
            self.list_parse_complete = complete
            self.list_parse_detail = '' if complete else '37 頁中有 1 頁抽不到文字（第 12 頁）'

        def download_pdf_list(self):
            return str(pdf)

        def parse_pdf_list(self, path):
            return dict(mapping)

        def extract_folder_id_from_url(self, url):
            # 回得出 folder id，_resolve_indirect_links 才不會被叫去連外
            return url.split('folders/')[1] if 'folders/' in url else ''

    from geobingan_sync.steps import sync_permits as sp
    monkeypatch.setattr(sp, 'PermitSync', _PS)

    class _Reader:
        def __init__(self, f):
            self.pages = []

    import pypdf
    monkeypatch.setattr(pypdf, 'PdfReader', _Reader)


_URL = 'https://drive.google.com/drive/folders/FID_A'


def test_the_gov_fetch_reports_list_members_not_just_parsed_links(monkeypatch, tmp_path):
    _fake_gov_pdf(monkeypatch, tmp_path, {'110建字第0001號': _URL},
                  ['110建字第0001號', '110建字第0002號'])
    results, membership = mp.fetch_gov_pdf_data()
    assert set(results) == {'110建字第0001號'}, '合併層只能走訪有連結的案'
    assert membership.permits == frozenset({'110建字第0001號', '110建字第0002號'}), (
        '清單成員要取 permits_in_list；取解析結果就是把我方解析能力當成對方清單')
    assert membership.complete is True and membership.conclusive is True


def test_a_parsed_permit_missing_from_the_list_is_kept_and_announced(
        monkeypatch, tmp_path, capsys):
    """不變式反了（有連結卻不在清單成員裡）＝兩邊正規化分叉，要出聲且偏寬。"""
    _fake_gov_pdf(monkeypatch, tmp_path, {'110建字第0001號': _URL},
                  ['110建字第0002號'])
    _, membership = mp.fetch_gov_pdf_data()
    assert membership.permits == frozenset({'110建字第0001號', '110建字第0002號'}), (
        '偏寬：寧可少判一筆已離開')
    assert '不在清單成員集合裡' in capsys.readouterr().out, '靜默就等於把 bug 放回來'


# ───────── 部分抽頁：集合是下限，不可以用來判「已離開清單」 ─────────

def test_a_partial_list_can_still_say_who_is_still_listed():
    """在下限集合裡找到的案，確實在清單上——「是」照判。

    全部降級成未取得也會讓「不可誤判已離開」那條綠，但那等於把本來可以斷定的
    14 筆資訊丟掉（這張表要處理的正是那幾筆）。
    """
    rows = mp.url_404_rows(_DEAD, _partial({'A建字第0001號'}))
    got = {r[0]: r[4] for r in rows}
    assert got == {'A建字第0001號': '是', 'B建字第0002號': mp.LIST_UNKNOWN}, (
        f'實際 {got}：找到的要判是，沒找到的只能說不知道')


def test_a_partial_list_never_says_anyone_left():
    """同一份測資，完整抽到時 B 是「否」、部分抽頁時只能是未取得。"""
    partial = {r[0]: r[4] for r in mp.url_404_rows(_DEAD, _partial({'A建字第0001號'}))}
    complete = {r[0]: r[4] for r in mp.url_404_rows(_DEAD, _complete({'A建字第0001號'}))}
    assert partial['B建字第0002號'] == mp.LIST_UNKNOWN
    assert complete['B建字第0002號'] == '否', (
        '對照組：完整抽到時仍然要判得出「已離開清單」')


def test_a_bare_set_is_refused_at_the_boundary():
    """裸集合會被當成完整清單，所以擋在型別上，不靠呼叫點自律。"""
    import pytest
    with pytest.raises(TypeError):
        mp.url_404_rows(_DEAD, {'A建字第0001號'})
    with pytest.raises(TypeError):
        mp._write_url_404_csv(_DEAD, frozenset({'A建字第0001號'}))


def test_the_csv_says_the_pages_were_incomplete(tmp_path, capsys):
    out = tmp_path / 'gov_pdf_url_404.csv'
    mp._write_url_404_csv(_DEAD, _partial({'A建字第0001號'}), out_path=str(out))
    text = out.read_text(encoding='utf-8-sig')
    rows = list(csv.reader(text.splitlines()))
    assert rows[1][4] == '是' and rows[2][4] == mp.LIST_UNKNOWN
    tail = [c for r in rows[-2:] for c in r if c]
    assert any('抽頁不完整' in c for c in tail), f'摘要要講原因，實際 {tail}'
    assert any('第 12 頁' in c for c in tail), '要講是哪幾頁'
    assert not any('已離開清單' in c for c in tail), (
        '抽頁不完整時不可以宣告任何一案已離開')
    assert '抽到部分頁面' in capsys.readouterr().out, '降級要在日誌裡看得見'


def test_still_listed_rows_come_first_even_when_partial():
    dead = {
        'A建字第0001號': dict(_DEAD['B建字第0002號']),
        'Z建字第0999號': dict(_DEAD['A建字第0001號']),
    }
    rows = mp.url_404_rows(dead, _partial({'Z建字第0999號'}))
    assert [r[0] for r in rows] == ['Z建字第0999號', 'A建字第0001號']
    assert [r[4] for r in rows] == ['是', mp.LIST_UNKNOWN]


def test_a_partial_parse_reaches_the_table_through_production(monkeypatch):
    """production 路徑：parse 不完整 → 列管表不出現任何「否」。"""
    rows = _run_build(monkeypatch, _DEAD,
                      {'A建字第0001號': {'source_url': 'u'}},
                      {'A建字第0001號'}, complete=False)
    verdicts = {r[0]: r[4] for r in rows}
    assert verdicts == {'A建字第0001號': '是', 'B建字第0002號': mp.LIST_UNKNOWN}, (
        f'實際 {verdicts}：抽頁不完整時 B 只是「沒抽到」，不是「已離開」')


def test_the_gov_fetch_carries_the_incompleteness(monkeypatch, tmp_path):
    """生產端：fetch_gov_pdf_data 要把 parse 的完整性一起帶出來。"""
    _fake_gov_pdf(monkeypatch, tmp_path, {'110建字第0001號': _URL},
                  ['110建字第0001號'], complete=False)
    _, membership = mp.fetch_gov_pdf_data()
    assert membership.complete is False and membership.conclusive is False, (
        '漏帶就等於下游把下限當成完整清單')
    assert '第 12 頁' in membership.detail
