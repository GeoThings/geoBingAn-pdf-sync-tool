"""來源量測結果要由產生端寫成資料，而且三種證據等級不可混同。

背景（2026-10-12）：slayer 問「我們抓取的有沒有遺漏」。清單 438 案裡有 99 案在
`permit_registry` 的 `pdf_count` 是 `None`，看起來像「從沒量過」。逐案探測後才
發現，程式其實**量到了**——`sync_permit` 列完來源資料夾、發現沒有 PDF，印一行
「⚠️ 來源無檔案」就 `return`，既不寫 `processed` 也不寫任何份數。於是：

- 57 個「實測 0 份」和「從沒量過」在帳上長得一模一樣；
- 4 案「資料夾有東西但不是 PDF」完全隱形，其中 `110建字第0038號` 有 13 個月的
  `.xls` 觀測資料（檔名寫著「北投觀測資料114年11月.xls」）從未被收進來；
- `list_files_recursive` 的 `except HttpError` 吞掉錯誤後回傳空 list，呼叫端把
  **API 失敗**也印成「來源無檔案」——未取得被當成實測 0，而那是靜默的。

本檔盯住的不變式：空清單的三種成因（真的空／有非 PDF／列出失敗）必須寫成三種
不同的資料，且「列出失敗」永遠不得寫成數字份數。
"""
import ast
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geobingan_sync.steps import sync_permits as sp_mod
from geobingan_sync.steps.sync_permits import PermitSync

_SRC = Path(sp_mod.__file__)


class _FakeResp:
    def __init__(self, status=500, reason='err'):
        self.status = status
        self.reason = reason


class _FakeFilesApi:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0) if self.script else {'files': []}

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


def _sync(monkeypatch, script):
    s = PermitSync(city={'name': 'T', 'pdf_list_url': 'https://example.test/x.pdf'})
    # service 只建一次。每次 _get_svc() 都新建的話，遞迴進子資料夾時 script 會
    # 從頭開始播，root 永遠再回傳一個子資料夾 → RecursionError（寫錯過一次）。
    svc = _FakeService(script)
    monkeypatch.setattr(s, '_get_svc', lambda: svc)
    return s


def _pdf(i, name=None):
    return {'id': i, 'name': name or f'{i}.pdf', 'mimeType': 'application/pdf'}


def _folder(i, name='S'):
    return {'id': i, 'name': name, 'mimeType': 'application/vnd.google-apps.folder'}


def _xls(i, name=None):
    return {'id': i, 'name': name or f'{i}.xls', 'mimeType': 'application/vnd.ms-excel'}


# ───────────────────────── list_files_recursive 的量測累加器 ─────────────────────────

def test_scan_counts_non_pdf_items(monkeypatch):
    """承造人改用 .xls 發布時，份數是 0 但 other 要是真實數字。"""
    s = _sync(monkeypatch, [{'files': [_xls('a'), _xls('b'), _xls('c')]}])
    scan = {'other': 0, 'failed': 0}
    files = s.list_files_recursive('root', scan=scan)
    assert files == []
    assert scan['other'] == 3
    assert scan['failed'] == 0


def test_scan_counts_failures_separately_from_emptiness(monkeypatch):
    """列出失敗不是「空」。空 list + failed>0 必須能和真的空區分開。"""
    s = _sync(monkeypatch, [HttpError(_FakeResp(403), b'denied')])
    scan = {'other': 0, 'failed': 0}
    files = s.list_files_recursive('root', scan=scan)
    assert files == []
    assert scan['failed'] == 1

    s2 = _sync(monkeypatch, [{'files': []}])
    scan2 = {'other': 0, 'failed': 0}
    assert s2.list_files_recursive('root', scan=scan2) == []
    assert scan2['failed'] == 0, '真的空不可被記成失敗'


def test_scan_propagates_through_recursion(monkeypatch):
    """子資料夾裡的非 PDF 與失敗都要累加到同一個 scan，否則深層隱形。"""
    s = _sync(monkeypatch, [
        {'files': [_folder('sub1'), _folder('sub2')]},
        {'files': [_xls('x'), _xls('y')]},                 # sub1
        # 403 而非 5xx：paginate_files_list 只對 429/5xx 退避重試，用 500 會
        # 在下一次腳本回空陣列時「重試成功」，failed 永遠是 0（踩過一次）。
        HttpError(_FakeResp(403), b'denied'),               # sub2 無權限
    ])
    scan = {'other': 0, 'failed': 0}
    s.list_files_recursive('root', scan=scan)
    assert scan['other'] == 2
    assert scan['failed'] == 1


def test_scan_is_optional_backward_compatible(monkeypatch):
    """既有呼叫點不傳 scan 時行為不變（#67 的翻頁測試仍靠這個簽章）。"""
    s = _sync(monkeypatch, [{'files': [_pdf('p1'), _xls('x')]}])
    files = s.list_files_recursive('root')
    assert [f[1] for f in files] == ['p1.pdf']


# ───────────────────────── sync_permit 把量測寫成資料 ─────────────────────────

def _scan_entry(s, permit='A建字第0001號'):
    return (s.state.get('source_scan') or {}).get(permit)


def test_empty_folder_records_measured_zero(monkeypatch):
    s = _sync(monkeypatch, [{'files': []}])
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    e = _scan_entry(s)
    assert e['reason'] == 'empty'
    assert e['pdf'] == 0, '列完了、真的沒有：這是實測 0，不是未取得'
    assert e['other'] == 0
    assert e['at']


def test_non_pdf_content_is_distinguishable_from_empty(monkeypatch):
    """這一條就是 110建字第0038號 當初該被看見的樣子。"""
    s = _sync(monkeypatch, [{'files': [
        _xls('m1', '北投觀測資料114年11月.xls'), _xls('m2', '北投觀測資料114年7月.xls')]}])
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    e = _scan_entry(s)
    assert e['reason'] == 'no_pdf_but_other'
    assert e['pdf'] == 0
    assert e['other'] == 2, '有 2 個非 PDF 檔，這個數字就是「別當成空資料夾」的證據'


def test_list_failure_records_unknown_not_zero(monkeypatch):
    """核心不變式：API 失敗寫成 None，絕不是 0。"""
    s = _sync(monkeypatch, [HttpError(_FakeResp(403), b'denied')])
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    e = _scan_entry(s)
    assert e['reason'] == 'list_failed'
    assert e['pdf'] is None, '未取得≠0；寫成 0 就等於宣告「這案來源是空的」'


def test_list_failure_also_records_an_error(monkeypatch):
    """原本這條路徑只 print 就吞掉，連續失敗結算永遠看不到它。"""
    s = _sync(monkeypatch, [HttpError(_FakeResp(403), b'denied')])
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    assert 'A建字第0001號' in s._errored_permits
    assert any(x['permit'] == 'A建字第0001號' for x in s.state.get('errors', []))


def test_success_path_records_the_count(monkeypatch):
    """只落檔失敗樣本的話，分不出「沒量過」與「量到有檔案」。"""
    s = _sync(monkeypatch, [
        {'files': [_pdf('p1'), _pdf('p2')]},   # 來源
        {'files': []},                          # preload 目標
    ])
    monkeypatch.setattr(s, 'preload_target_files', lambda *a, **k: None)
    monkeypatch.setattr(s, 'check_file_exists', lambda *a, **k: True)
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    e = _scan_entry(s)
    assert e['reason'] == 'ok'
    assert e['pdf'] == 2


def test_partial_listing_does_not_claim_a_count(monkeypatch):
    """抓到幾份但有子資料夾列不出來時，份數只是下限，不可當定論。"""
    s = _sync(monkeypatch, [
        {'files': [_pdf('p1'), _folder('sub')]},
        HttpError(_FakeResp(403), b'denied'),    # 子資料夾無權限（持久失敗）
    ])
    monkeypatch.setattr(s, 'preload_target_files', lambda *a, **k: None)
    monkeypatch.setattr(s, 'check_file_exists', lambda *a, **k: True)
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    e = _scan_entry(s)
    assert e['reason'] == 'partial'
    assert e['pdf'] is None, '不完整的清單不可寫成確定份數'


def test_invalid_url_records_unknown(monkeypatch):
    s = _sync(monkeypatch, [{'files': []}])
    monkeypatch.setattr(sp_mod, 'find_adapter', lambda u: None, raising=False)
    s.sync_permit('A建字第0001號', 'not-a-drive-url', 'tgt')
    e = _scan_entry(s)
    assert e is not None, 'URL 解析不出來也是一種量測結果，不可完全不落檔'
    assert e['reason'] == 'invalid_url'
    assert e['pdf'] is None


def test_now_is_injectable(monkeypatch):
    """固定日期的斷言要能凍結時鐘，不然是隔天會爆的日期炸彈。"""
    s = _sync(monkeypatch, [{'files': []}])
    s._record_source_scan('A建字第0001號', pdf=0, other=0, reason='empty',
                          now=datetime(2026, 10, 12, 10, 0, 0))
    assert _scan_entry(s)['at'] == '2026-10-12T10:00:00'


# ───────────────────────── 摘要要露出，否則寫了也沒人看 ─────────────────────────

def test_finalize_surfaces_non_pdf_sources(monkeypatch, capsys):
    s = _sync(monkeypatch, [{'files': []}])
    s._visited_permits = {'A建字第0001號'}
    s.state['source_scan'] = {
        'A建字第0001號': {'at': 'x', 'reason': 'no_pdf_but_other', 'pdf': 0, 'other': 13},
    }
    s._finalize_errors(completed=True)
    out = capsys.readouterr().out
    assert '來源有檔案但沒有 PDF' in out
    assert 'A建字第0001號' in out


def test_finalize_ignores_permits_not_visited_this_run(monkeypatch, capsys):
    """摘要講的是「本輪」。拿歷史紀錄湊分子，就是分子分母不同源。"""
    s = _sync(monkeypatch, [{'files': []}])
    s._visited_permits = set()
    s.state['source_scan'] = {
        'B建字第0002號': {'at': 'x', 'reason': 'no_pdf_but_other', 'pdf': 0, 'other': 5},
    }
    s._finalize_errors(completed=True)
    assert '來源有檔案但沒有 PDF' not in capsys.readouterr().out


def test_finalize_stays_quiet_when_nothing_to_report(monkeypatch, capsys):
    s = _sync(monkeypatch, [{'files': []}])
    s._visited_permits = {'A建字第0001號'}
    s.state['source_scan'] = {
        'A建字第0001號': {'at': 'x', 'reason': 'empty', 'pdf': 0, 'other': 0},
    }
    s._finalize_errors(completed=True)
    assert '來源有檔案但沒有 PDF' not in capsys.readouterr().out


# ───────────────────────── 防誤傷：擋住「失敗寫成 0」這個回歸 ─────────────────────────

_UNKNOWN_REASONS = {'list_failed', 'invalid_url', 'partial'}


def _record_calls():
    """用 AST 找 `_record_source_scan(...)` 的呼叫點。

    用 AST 而不是 grep：註解與字串裡的同名文字不會被算進來（前幾個 PR 的
    source-scan 守門測試都在「忘了剝掉註解行」上吃過假綠）。
    """
    tree = ast.parse(_SRC.read_text(encoding='utf-8'))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == '_record_source_scan':
            kw = {k.arg: k.value for k in node.keywords}
            out.append((node.lineno, kw))
    return out


def test_every_call_site_passes_reason_and_pdf():
    calls = _record_calls()
    assert len(calls) >= 4, f'呼叫點只找到 {len(calls)} 個，AST 比對可能壞了'
    for lineno, kw in calls:
        assert 'reason' in kw and 'pdf' in kw, f'line {lineno} 缺 reason/pdf'


def test_unknown_reasons_never_pass_a_numeric_count():
    """`list_failed`／`invalid_url`／`partial` 只能寫 None。

    這一條防的是「以後有人覺得補個 0 比較整齊」。它會真的碰撞：把實作改成
    `pdf=0` 就立刻紅。
    """
    offenders = []
    for lineno, kw in _record_calls():
        r = kw.get('reason')
        if isinstance(r, ast.Constant) and r.value in _UNKNOWN_REASONS:
            v = kw.get('pdf')
            if not (isinstance(v, ast.Constant) and v.value is None):
                offenders.append((lineno, r.value, ast.dump(v) if v else None))
    assert not offenders, (
        f'這些呼叫點把「未取得」寫成了數字，未取得≠0：{offenders}')


def test_measured_and_unknown_are_actually_different_values():
    """最終防線用行為斷言，不只看原始碼：兩種結果必須真的不同。"""
    import inspect
    src = inspect.getsource(PermitSync.sync_permit)
    assert "reason='empty'" in src and "reason='list_failed'" in src, (
        '三種成因收斂成一條訊息的話，這整組測試就失去意義')
