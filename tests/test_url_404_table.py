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
    rows = mp.url_404_rows(_DEAD, {'A建字第0001號', 'C建字第0003號'})
    got = {r[0]: r[4] for r in rows}
    assert got == {'A建字第0001號': '是', 'B建字第0002號': '否'}, (
        '兩種 404 要分開；混在一起就會把「已離開清單」的案拿去請建管處更新')


def test_alive_permits_stay_out_of_the_table():
    rows = mp.url_404_rows(_DEAD, {'A建字第0001號', 'C建字第0003號'})
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
    rows = mp.url_404_rows(dead, {'Z建字第0999號'})
    assert [r[0] for r in rows] == ['Z建字第0999號', 'A建字第0001號', 'M建字第0500號'], (
        '仍在清單的要排最前面，其餘各組內再按字號')
    assert [r[4] for r in rows] == ['是', '否', '否']


def test_the_recheck_date_is_per_permit_not_the_output_date():
    rows = mp.url_404_rows(_DEAD, {'A建字第0001號'})
    by = {r[0]: r[5] for r in rows}
    assert by['A建字第0001號'] == '2026-10-08', (
        '要寫這一案自己的重驗日；寫成輸出當天就會把批次資訊整張抹平')
    assert by['B建字第0002號'] == '無紀錄', '沒有重驗過就要說沒有，不要填一個日期'


def test_an_empty_gov_list_does_not_claim_everyone_left():
    """清單整份抓不到時（gov_permits 空），不可以把每一列都講成「已離開」。

    這條是反面：空集合下兩列都會是「否」——那在資料上是真的（本輪清單裡沒有
    它們），但摘要行必須把筆數講出來，人才看得出「33 筆全部離開」這種異常。
    """
    rows = mp.url_404_rows(_DEAD, set())
    assert [r[4] for r in rows] == ['否', '否']


def test_the_csv_has_the_new_column_and_a_summary_line(tmp_path):
    out = tmp_path / 'gov_pdf_url_404.csv'
    mp._write_url_404_csv(_DEAD, {'A建字第0001號'}, out_path=str(out))
    rows = list(csv.reader(out.read_text(encoding='utf-8-sig').splitlines()))
    assert rows[0][4] == '本輪仍在市府清單'
    assert rows[0][5] == '最後重驗日'
    assert rows[1][0] == 'A建字第0001號' and rows[1][4] == '是'
    assert rows[2][0] == 'B建字第0002號' and rows[2][4] == '否'
    tail = [c for r in rows[3:] for c in r if c]
    assert any('輸出日' in c for c in tail), '檔案產生日改成獨立一列，不混進逐案欄位'
    assert any('仍在清單 1 筆' in c for c in tail), '摘要要直接講兩種各幾筆'


def test_the_writer_passes_the_current_list_through(monkeypatch, tmp_path):
    """守門：build_registry 要把**本輪**的清單成員交給列管表。

    漏傳就等於整張表都是「否」，而那會把「建管處要更新的 14 筆」藏起來。
    """
    seen = {}
    monkeypatch.setattr(mp, '_write_url_404_csv',
                        lambda reg, gov_permits=frozenset(), **k:
                        seen.update(reg=reg, gov=set(gov_permits)))

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
                        lambda city=None: {'A建字第0001號': {'source_url': 'u'}})
    monkeypatch.setattr(mp, 'fetch_source_folder_names',
                        lambda gov, svc, prior_registry=None: ({}, {}, '2026-10-14T10:00:00'))
    monkeypatch.setattr(mp, 'fetch_drive_pdf_names', lambda svc: ({}, True))
    monkeypatch.setattr(mp, 'fetch_api_projects', lambda: [])
    monkeypatch.setattr(mp, 'fetch_api_report_categories', lambda: {})
    monkeypatch.setattr(mp, 'fetch_live_alerts', lambda: {})
    monkeypatch.setattr(mp, 'commit_registry_with_deaths', lambda reg, prior, **k: None)
    mp.build_registry()
    assert seen['gov'] == {'A建字第0001號'}, (
        f"列管表拿到的清單成員是 {seen.get('gov')}，不是本輪 gov_data 的鍵")
