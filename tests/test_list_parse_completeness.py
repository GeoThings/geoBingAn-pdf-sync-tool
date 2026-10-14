"""PDF 只抽到**部分頁面**時，那份建照集合是下限，不是「清單上只有這些」。

2026-10-14 實測這份清單：37 頁、每頁 12 案（最後兩頁 10 和 8）、沒有任何一頁是
空白的，最少的一頁也有 1091 個字元。所以「某頁抽到 0 字」是抽取失效，不是正常
版面——而掉一頁就是靜默少 8～12 案。

少掉的那些案會流到兩個地方下結論，兩邊都要擋住：
1. 清單指紋：報「移除 12 筆」（看起來像政府下架了建案），還把 last_changed 推到
   當天、重設停更時鐘——2026-09-30 那次誤報的傷害一模一樣。
2. 失效 URL 列管表：那些案被標成「已離開清單」，我們就會去查一個根本沒離開的案。

這是 #119 那個 `set(gov_data)` 的同形錯誤：把我方的抽取能力記成對方的清單內容。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta

from geobingan_sync.list_fingerprint import (
    IncompletePermitList, ListFingerprint, assess)
import geobingan_sync.list_fingerprint as lf
import geobingan_sync.steps.sync_permits as sp

T0 = datetime(2026, 10, 14, 10, 0, 0)

_ROW = ('{permit} 某建築師事務所 某營造股份有限公司 '
        'https://drive.google.com/drive/folders/FID_{n}\n')


def _page(*permits):
    """一頁的文字：每案一行，結尾帶頁碼（production 會 strip 掉）。"""
    return ''.join(_ROW.format(permit=p, n=i) for i, p in enumerate(permits, 1))


class _Page:
    def __init__(self, text, boom=None):
        self._text, self._boom = text, boom
        self.extracted = 0          # 這一頁被抽了幾次（見「只抽一次」那條）

    def extract_text(self):
        self.extracted += 1
        if self._boom:
            raise self._boom
        return self._text


def _parse(monkeypatch, tmp_path, pages):
    """跑真正的 parse_pdf_list，只換掉 pypdf。"""
    pdf = tmp_path / 'list.pdf'
    pdf.write_bytes(b'%PDF-1.4 fake')

    class _Reader:
        def __init__(self, f):
            self.pages = list(pages)

    monkeypatch.setattr(sp.pypdf, 'PdfReader', _Reader)
    ps = sp.PermitSync()
    mapping = ps.parse_pdf_list(str(pdf))
    return ps, mapping


# ───────── 生產端：抽不到的頁要被記下來 ─────────

def test_a_full_parse_is_complete(monkeypatch, tmp_path):
    """對照組：每頁都抽到文字就是完整，detail 要空的。

    少了這條，把 list_parse_complete 寫死成 False 也會「全綠」——那是另一個
    方向的靜默（每輪都說不完整＝永久降級，跟沒有告警一樣沒用）。
    """
    ps, mapping = _parse(monkeypatch, tmp_path, [
        _Page(_page('110建字第0001號', '110建字第0002號')),
        _Page(_page('110建字第0003號')),
    ])
    assert ps.list_parse_complete is True
    assert ps.list_parse_detail == ''
    assert ps.permits_in_list == ['110建字第0001號', '110建字第0002號',
                                  '110建字第0003號']
    assert len(mapping) == 3


def test_a_page_with_no_text_marks_the_parse_incomplete(monkeypatch, tmp_path):
    """實測這份 PDF 每頁都有 1091 字以上，所以 0 字＝抽取失效。"""
    ps, mapping = _parse(monkeypatch, tmp_path, [
        _Page(_page('110建字第0001號')),
        _Page(''),                                   # 抽不到文字的那一頁
        _Page(_page('110建字第0003號')),
    ])
    assert ps.list_parse_complete is False, '少了一頁就不是完整的清單'
    assert '第 2' in ps.list_parse_detail and '3 頁' in ps.list_parse_detail, (
        f'要說是哪幾頁，實際：{ps.list_parse_detail}')
    # 其餘頁的資料照用：這份集合是下限，不是零
    assert ps.permits_in_list == ['110建字第0001號', '110建字第0003號']
    assert len(mapping) == 2


def test_a_page_that_raises_does_not_kill_the_whole_parse(monkeypatch, tmp_path):
    """單頁拋例外只讓那一頁算缺，其餘 36 頁的資料照用。

    原本是一行 genexp，單頁拋例外會讓整步死掉；而 `or ''` 又把「抽不到文字」
    吞成空字串。兩種都要走到「這輪不完整」這條路上。
    """
    ps, mapping = _parse(monkeypatch, tmp_path, [
        _Page(_page('110建字第0001號')),
        _Page('', boom=ValueError('cannot decode page')),
        _Page(_page('110建字第0003號')),
    ])
    assert ps.list_parse_complete is False
    assert '第 2' in ps.list_parse_detail
    assert ps.permits_in_list == ['110建字第0001號', '110建字第0003號']
    assert len(mapping) == 2


def test_the_incomplete_warning_names_the_pages(monkeypatch, tmp_path, capsys):
    ps, _ = _parse(monkeypatch, tmp_path, [
        _Page(_page('110建字第0001號')), _Page(''), _Page(''),
    ])
    out = capsys.readouterr().out
    assert '抽不到' in out and '下限' in out, (
        '降級要在日誌裡看得見；靜默降級就是把誤判偷偷帶回來')
    assert ps.list_parse_detail.count('、') == 1, '多頁要一起列出來'


# ───────── 指紋：下限不可以寫成清單內容 ─────────

def _fp(tmp_path):
    return ListFingerprint(tmp_path / 'f.json')


def test_an_incomplete_parse_does_not_overwrite_the_baseline(tmp_path):
    """有基線時保留上一次完整觀測，只記下這輪不完整。"""
    fp = _fp(tmp_path)
    fp.update('v1.pdf', '動態', ['a', 'b', 'c'], now=T0)
    before = fp.load()

    changed, summary, state = fp.update('v1.pdf', '動態', ['a'], now=T0 + timedelta(days=1),
                                        complete=False,
                                        incomplete_detail='37 頁中有 1 頁抽不到文字（第 12 頁）')
    assert changed is False and summary is None, '部分抽頁不是「清單已更新」'
    assert state['permits'] == before['permits'], '內容欄位要保留上一次完整觀測'
    assert state['permit_count'] == 3 and state['sha256'] == before['sha256']
    assert state['last_changed'] == before['last_changed'], (
        '不可推進 last_changed——那會把疑似停更的時鐘重設掉')
    assert state['parse_incomplete'] is True
    assert state['incomplete_parse_lower_bound'] == 1, '本輪只看到幾筆要記下來'
    assert '第 12 頁' in state['incomplete_parse_detail']


def test_an_incomplete_parse_never_reports_removals(tmp_path):
    """下限拿去比對就會報「移除 N 筆」——那是 2026-09-30 的誤報形狀。"""
    fp = _fp(tmp_path)
    fp.update('v1.pdf', '動態', [f'1{i:02d}建字第0001號' for i in range(10)], now=T0)
    changed, summary, state = fp.update('v1.pdf', '動態', ['100建字第0001號'],
                                        now=T0 + timedelta(days=1), complete=False,
                                        incomplete_detail='第 2 頁')
    assert changed is False
    assert summary is None
    assert not state.get('pending_notices'), '不可排入任何變更通知'
    assert '移除' not in (state.get('last_change_summary') or '')


def test_an_incomplete_parse_with_no_baseline_fails_closed(tmp_path):
    """沒有基線時不可以用下限建基線：下一輪抽完整就會誤報「新增 N 筆」。"""
    fp = _fp(tmp_path)
    with pytest.raises(IncompletePermitList):
        fp.update('v1.pdf', '動態', ['a'], now=T0, complete=False,
                  incomplete_detail='第 2 頁')
    assert not (tmp_path / 'f.json').exists(), '連檔案都不該建立'


def test_recovery_after_an_incomplete_round_is_not_a_change(tmp_path):
    """抽頁恢復正常的那一輪，內容和上一次完整觀測相同 → 不是變更。

    這條是上面那條的另一半：基線沒被下限覆蓋，恢復才不會誤報「新增」。
    """
    fp = _fp(tmp_path)
    fp.update('v1.pdf', '動態', ['a', 'b', 'c'], now=T0)
    fp.update('v1.pdf', '動態', ['a'], now=T0 + timedelta(days=1), complete=False,
              incomplete_detail='第 2 頁')
    changed, summary, state = fp.update('v1.pdf', '動態', ['a', 'b', 'c'],
                                        now=T0 + timedelta(days=2))
    assert changed is False and summary is None, '恢復不是新增'
    assert state['parse_incomplete'] is False, '抽得完整就要把旗標關掉'
    assert state['incomplete_parse_at'], '但歷史要留著（forensic）'


def test_a_genuine_removal_is_still_reported_when_complete(tmp_path):
    """對照組：完整抽到時，真的移除仍然要報出來。

    少了這條，把 complete 一律當成 False（永久降級）也會全綠。
    """
    fp = _fp(tmp_path)
    fp.update('v1.pdf', '動態', ['a', 'b', 'c'], now=T0)
    changed, summary, _ = fp.update('v1.pdf', '動態', ['a', 'b'],
                                    now=T0 + timedelta(days=1))
    assert changed is True and '移除 1 筆' in summary


def test_health_check_reports_an_incomplete_round(tmp_path):
    fp = _fp(tmp_path)
    fp.update('v1.pdf', '動態', ['a', 'b', 'c'], now=T0)
    _, _, state = fp.update('v1.pdf', '動態', ['a'], now=T0 + timedelta(days=1),
                            complete=False, incomplete_detail='第 12 頁')
    level, msg = assess(state, now=T0 + timedelta(days=1))
    assert level == 'error', '一輪少掉 8～12 案不可以只是 ok'
    assert '第 12 頁' in msg and '下限' in msg
    # 恢復後就不該再亮
    _, _, ok_state = fp.update('v1.pdf', '動態', ['a', 'b', 'c'],
                               now=T0 + timedelta(days=2))
    assert assess(ok_state, now=T0 + timedelta(days=2))[0] == 'ok'


def test_sending_a_notice_does_not_silence_the_incomplete_alarm(tmp_path):
    """旗標每輪實寫，不靠「時間戳相等」去推論。

    clear_pending 會推進 last_checked：若用 incomplete_parse_at == last_checked
    判定，通知送出的那一輪告警就會靜默消失。
    """
    fp = _fp(tmp_path)
    fp.update('v1.pdf', '動態', ['a', 'b'], now=T0)
    fp.update('v1.pdf', '動態', ['a', 'b', 'c'], now=T0 + timedelta(days=1))  # 產生 pending
    fp.update('v1.pdf', '動態', ['a'], now=T0 + timedelta(days=2), complete=False,
              incomplete_detail='第 2 頁')
    state = fp.clear_pending(now=T0 + timedelta(days=2, hours=1))
    assert assess(state, now=T0 + timedelta(days=2, hours=1))[0] == 'error', (
        '通知送達不代表抽頁變完整了')


# ───────── production 接線：完整性要真的傳到指紋 ─────────

def test_the_sync_step_passes_completeness_to_the_fingerprint(tmp_path, monkeypatch):
    """守門：parse_pdf_list 量到的完整性要一路傳進 ListFingerprint.update。

    漏傳就是部分抽頁被當成完整，於是誤報「移除 N 筆」並重設停更時鐘。
    """
    seen = {}

    class _FP:
        def update(self, label, source, permits, **kw):
            seen.update(permits=list(permits), kw=kw)
            return False, None, {'label': label, 'source': source,
                                 'permit_count': len(set(permits)),
                                 'pending_notices': []}

    monkeypatch.setattr(lf, 'ListFingerprint', lambda *a, **k: _FP())
    ps = sp.PermitSync.__new__(sp.PermitSync)
    ps.list_label, ps.list_source = 'v1.pdf', '動態'
    ps.permit_mapping = {'a': ''}
    ps.permits_in_list = ['a']
    ps.list_parse_complete = False
    ps.list_parse_detail = '37 頁中有 1 頁抽不到文字（第 12 頁）'
    ps._record_list_fingerprint()
    assert seen['kw'].get('complete') is False, (
        f"指紋沒收到本輪的完整性：{seen.get('kw')}")
    assert '第 12 頁' in (seen['kw'].get('incomplete_detail') or '')


# ───────── production：壞頁要能一路走到 partial membership（#120 review P1） ─────────

def _gov_fetch_with_pages(monkeypatch, tmp_path, pages):
    """跑真正的 fetch_gov_pdf_data + parse_pdf_list，只換掉下載與 pypdf。

    `_fake_gov_pdf` 那組測試把整個 PermitSync 換掉，所以**看不到**
    fetch_gov_pdf_data 自己在解析之後又把整份 PDF 抽一次文字那段——那一行是
    舊寫法、沒有逐頁容錯，同一個壞頁照樣會讓整步中止，剛算好的「部分抽頁」
    根本傳不到列管表。這個 harness 專門走那條路。
    """
    import geobingan_sync.steps.match_permits as mp

    pdf = tmp_path / 'list.pdf'
    pdf.write_bytes(b'%PDF-1.4 fake')

    class _Reader:
        def __init__(self, f):
            self.pages = list(pages)

    monkeypatch.setattr(sp.pypdf, 'PdfReader', _Reader)
    monkeypatch.setattr(sp.PermitSync, 'download_pdf_list',
                        lambda self, *a, **k: str(pdf))
    return mp.fetch_gov_pdf_data()


def test_the_pdf_is_only_extracted_once(monkeypatch, tmp_path):
    """整份 PDF 只能抽一次文字。

    第二條抽取路徑就是這個 bug 的來源：它自己一份 `p.extract_text() or ''`，
    沒有逐頁容錯，壞頁從那裡拋出去讓整步中止。把它包一層 try/except 並不算修好
    ——重複的抽取路徑只會再漂走一次（這一發注入當時沒有任何測試抓到）。所以
    直接把「每頁只抽一次」釘住，不管第二份是怎麼寫的。
    """
    pages = [_Page(_page('110建字第0001號')), _Page(_page('110建字第0003號'))]
    _gov_fetch_with_pages(monkeypatch, tmp_path, pages)
    assert [pg.extracted for pg in pages] == [1, 1], (
        f'每頁被抽了 {[pg.extracted for pg in pages]} 次；'
        '第二條抽取路徑要整段刪掉，不是包一層 try/except')


def test_a_bad_page_reaches_the_table_as_partial_not_as_a_crash(monkeypatch, tmp_path):
    results, membership = _gov_fetch_with_pages(monkeypatch, tmp_path, [
        _Page(_page('110建字第0001號')),
        _Page('', boom=ValueError('cannot decode page')),
        _Page(_page('110建字第0003號')),
    ])
    assert membership.complete is False, '壞頁要一路傳成 partial'
    assert membership.conclusive is False
    assert membership.permits == frozenset({'110建字第0001號', '110建字第0003號'})
    assert set(results) == {'110建字第0001號', '110建字第0003號'}, (
        '其餘頁的連結照用，合併層還是要能走訪它們')


def test_a_clean_pdf_still_reports_a_complete_list(monkeypatch, tmp_path):
    """對照組：沒有壞頁時照樣是完整清單（否則把 complete 寫死成 False 也會綠）。"""
    _, membership = _gov_fetch_with_pages(monkeypatch, tmp_path, [
        _Page(_page('110建字第0001號')), _Page(_page('110建字第0003號')),
    ])
    assert membership.complete is True and membership.conclusive is True
    assert membership.detail == ''
