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

2026-10-12 後續（Zhe 的範圍決定）：**圖片與 .xls 也要鏡像進共享雲端硬碟，但不會
上傳到 riskmap。** 於是一次掃描有三個數字而不是兩個——`pdf`（會上傳的份數）、
`other`（鏡像了的非 PDF）、`skipped`（連鏡像都不收的型別）。`pdf` 的意義不變，
而且**正是**它不可以被 13 個 .xls 撐大：那會讓「這案有報告在收」變成假的。
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
    def __init__(self, script, folder_state='alive'):
        self.script = list(script)
        self.calls = []
        #: `alive` / `trashed` / 直接給一個 HttpError 當成探測失敗。
        #: 預設 alive：既有測試講的都是「資料夾在、裡面有什麼」。
        self.folder_state = folder_state
        self.get_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        state = self.folder_state

        class _Req:
            def execute(self):
                if isinstance(state, Exception):
                    raise state
                return {'id': kwargs.get('fileId'), 'trashed': state == 'trashed'}

        return _Req()

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
    def __init__(self, script, folder_state='alive'):
        self._files = _FakeFilesApi(script, folder_state)

    def files(self):
        return self._files


def _sync(monkeypatch, script, folder_state='alive'):
    s = PermitSync(city={'name': 'T', 'pdf_list_url': 'https://example.test/x.pdf'})
    # service 只建一次。每次 _get_svc() 都新建的話，遞迴進子資料夾時 script 會
    # 從頭開始播，root 永遠再回傳一個子資料夾 → RecursionError（寫錯過一次）。
    svc = _FakeService(script, folder_state)
    monkeypatch.setattr(s, '_get_svc', lambda: svc)
    return s


def _pdf(i, name=None):
    return {'id': i, 'name': name or f'{i}.pdf', 'mimeType': 'application/pdf'}


def _folder(i, name='S'):
    return {'id': i, 'name': name, 'mimeType': 'application/vnd.google-apps.folder'}


def _xls(i, name=None):
    return {'id': i, 'name': name or f'{i}.xls', 'mimeType': 'application/vnd.ms-excel'}


# ───────────────────────── list_files_recursive 的量測累加器 ─────────────────────────

def test_non_pdf_files_are_now_mirrored(monkeypatch):
    """承造人改用 .xls 發布時，檔案要收進來（但 PDF 份數仍然是 0）。"""
    s = _sync(monkeypatch, [{'files': [_xls('a'), _xls('b'), _xls('c')]}])
    scan = {'skipped': 0, 'failed': 0}
    files = s.list_files_recursive('root', scan=scan)
    assert [f[1] for f in files] == ['a.xls', 'b.xls', 'c.xls']
    assert {f[4] for f in files} == {'application/vnd.ms-excel'}, (
        'mimeType 要帶回去，copy_file 的下載回退路徑得用它重建檔案')
    assert scan['skipped'] == 0, '收的型別不算進 skipped'
    assert scan['failed'] == 0


@pytest.mark.parametrize('name,mime,mirrored', [
    # 實測來源（2026-10-12 逐案探測四個建案的真實 mimeType）
    ('北投觀測資料114年11月.xls', 'application/vnd.ms-excel', True),
    ('富邦人壽承德大樓北基地_監測資料1131223.jpg', 'image/jpeg', True),
    ('3E6E9YEBEO.png', 'image/png', True),
    ('報告.pdf', 'application/pdf', True),
    # 上傳端把型別標成通用值是常態，只看 mimeType 會靜默漏掉
    ('觀測資料115年1月.xlsx', 'application/octet-stream', True),
    ('監測照片.JPG', 'application/octet-stream', True),
    # 不收的型別：留在來源，只記 skipped
    ('施工計畫書.doc', 'application/msword', False),
    ('全部資料.zip', 'application/zip', False),
    ('說明.txt', 'text/plain', False),
])
def test_mirror_allowlist_matches_the_real_sources(name, mime, mirrored):
    from geobingan_sync.steps.sync_permits import should_mirror
    assert should_mirror(name, mime) is mirrored


def test_unsupported_types_are_counted_as_skipped(monkeypatch):
    s = _sync(monkeypatch, [{'files': [
        {'id': 'd', 'name': '施工計畫書.doc', 'mimeType': 'application/msword'},
        _xls('x'),
    ]}])
    scan = {'skipped': 0, 'failed': 0}
    files = s.list_files_recursive('root', scan=scan)
    assert [f[1] for f in files] == ['x.xls']
    assert scan['skipped'] == 1, '不收的型別要記下來，不可無聲丟掉'


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
    """子資料夾裡的檔案與失敗都要帶回同一個 scan，否則深層隱形。"""
    s = _sync(monkeypatch, [
        {'files': [_folder('sub1'), _folder('sub2')]},
        {'files': [_xls('x'), {'id': 'z', 'name': 'a.zip',
                               'mimeType': 'application/zip'}]},   # sub1
        # 403 而非 5xx：paginate_files_list 只對 429/5xx 退避重試，用 500 會
        # 在下一次腳本回空陣列時「重試成功」，failed 永遠是 0（踩過一次）。
        HttpError(_FakeResp(403), b'denied'),               # sub2 無權限
    ])
    scan = {'skipped': 0, 'failed': 0}
    files = s.list_files_recursive('root', scan=scan)
    assert [f[1] for f in files] == ['x.xls'], '深層的 .xls 也要收'
    assert scan['skipped'] == 1
    assert scan['failed'] == 1


def test_scan_is_optional_backward_compatible(monkeypatch):
    """不傳 scan 時不可炸（#67 的翻頁測試仍靠這個簽章）。"""
    s = _sync(monkeypatch, [{'files': [_pdf('p1'), _xls('x')]}])
    files = s.list_files_recursive('root')
    assert [f[1] for f in files] == ['p1.pdf', 'x.xls']


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
    """這一條就是 110建字第0038號 的樣子：13 個月的 .xls，一份 PDF 都沒有。

    現在這些檔案會被鏡像，但 `pdf` 仍然是 0——riskmap 那邊確實一份都收不到，
    帳要照實記。把鏡像份數算進 `pdf` 就等於宣稱這案有報告在收。
    """
    s = _sync(monkeypatch, [{'files': [
        _xls('m1', '北投觀測資料114年11月.xls'), _xls('m2', '北投觀測資料114年7月.xls')]}])
    monkeypatch.setattr(s, 'preload_target_files', lambda *a, **k: None)
    copied = []
    monkeypatch.setattr(s, 'check_file_exists', lambda *a, **k: False)
    monkeypatch.setattr(s, 'copy_file',
                        lambda fid, tgt, name, path='', mime_type=None:
                        (copied.append((name, mime_type)) or 'new', tgt))
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    e = _scan_entry(s)
    assert e['reason'] == 'no_pdf_but_other'
    assert e['pdf'] == 0, 'riskmap 收不到任何一份，這個 0 是實測'
    assert e['other'] == 2, '鏡像了 2 個非 PDF 檔，這個數字是「別當成空資料夾」的證據'
    assert e['skipped'] == 0
    assert copied == [('北投觀測資料114年11月.xls', 'application/vnd.ms-excel'),
                      ('北投觀測資料114年7月.xls', 'application/vnd.ms-excel')], (
        '檔案要真的被複製，而且型別跟著來源走')


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
    import geobingan_sync.source_adapters as sa

    s = _sync(monkeypatch, [{'files': []}])
    # 要換的是 source_adapters 模組上的名字。sync_permit 是在函式內 import，
    # 換 sync_permits 的同名屬性是**空操作**（它從來不讀那裡），這支測試原本
    # 就是靠「真的 find_adapter 剛好也回 None」才綠的。
    monkeypatch.setattr(sa, 'find_adapter', lambda u: None)
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

# 分類從 production 匯入，不在測試裡抄一份——抄的那份不會跟著改，
# 守門測試就會在「以後新增一個未取得的 reason」時無聲放過。
_UNKNOWN_REASONS = sp_mod.UNKNOWN_SCAN_REASONS


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


def _reason_pdf_pairs(kw):
    """把一個呼叫點攤成 [(reason, pdf_node)]，條件運算式要攤成兩組。

    `reason='partial' if scan['failed'] else 'ok'` 在 AST 裡是 IfExp 而不是常數。
    舊版的守門測試只認常數，於是**兩個**呼叫點（sync_permit 成功路徑、整包取得
    失敗）被無聲跳過——測試名字寫著「every call site」，實際上沒在看那兩個。
    攤不開的形狀回 None，讓呼叫端失敗而不是跳過（跳過就是下一次的假綠）。
    """
    r, v = kw.get('reason'), kw.get('pdf')
    if isinstance(r, ast.Constant):
        return [(r.value, v)]
    if isinstance(r, ast.IfExp):
        # pdf 也分支時，兩邊的條件必須是**同一個**條件，否則配對出來的結論是假的。
        if isinstance(v, ast.IfExp) and ast.dump(v.test) != ast.dump(r.test):
            return None
        branches = [(r.body, _branch(v, 'body')), (r.orelse, _branch(v, 'orelse'))]
        if any(not isinstance(b, ast.Constant) for b, _ in branches):
            return None
        return [(b.value, p) for b, p in branches]
    return None


def _branch(node, which):
    """pdf 跟著 reason 一起分支時取對應那一側，否則整個節點對兩側都適用。

    非常數的運算式（`len(files)` 這種）留著原樣就好：靜態上唯一能判定的是
    「有沒有寫成字面的 None」，而那正是這組守門測試要看的那一件事。
    """
    return getattr(node, which) if isinstance(node, ast.IfExp) else node


def _delegated_calls():
    """用 `**self._scan_result(...)` 展開參數的呼叫點。

    這種呼叫點靜態上看不到 reason／pdf，但它們的值全部由 `_scan_result()`
    這一個純函式算出來——所以不是放過，而是**換一種更強的驗法**：下面
    `test_scan_result_is_consistent_for_every_combination` 把那個函式的輸入
    組合窮舉過一遍。另外執行期還有 `_record_source_scan()` 自己的一致性檢查。
    """
    tree = ast.parse(_SRC.read_text(encoding='utf-8'))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == '_record_source_scan'):
            for kw in node.keywords:
                if kw.arg is None and isinstance(kw.value, ast.Call) and (
                        getattr(kw.value.func, 'attr', None) == '_scan_result'):
                    out.append(node.lineno)
    return out


def test_every_call_site_passes_reason_and_pdf():
    delegated = _delegated_calls()
    assert delegated, (
        'sync_permit 應該有委派給 _scan_result 的呼叫點；一個都找不到的話，'
        '是 AST 比對壞了，不是程式碼變乾淨了')
    calls = [(ln, kw) for ln, kw in _record_calls() if ln not in delegated]
    assert len(delegated) == 2, (
        f'委派給 _scan_result 的呼叫點應該有 2 個（沒東西可鏡像那條、複製完那條），'
        f'找到 {len(delegated)} 個')
    assert len(calls) >= 6, (
        f'直接寫 reason／pdf 的呼叫點只找到 {len(calls)} 個，AST 比對可能壞了'
        '（預期 6：Drive 路徑的 invalid_url，整包路徑的取得失敗／ok／not_measured，'
        'adapter 逐檔的列檔失敗／成功）')
    for lineno, kw in calls:
        assert 'reason' in kw and 'pdf' in kw, f'line {lineno} 缺 reason/pdf'


def test_every_call_site_is_analysable_or_delegated():
    """看不懂的呼叫點要讓守門測試紅，不是讓它安靜跳過。

    這一條是上一版的真正漏洞：它當時只認常數 reason，兩個用條件運算式的呼叫點
    因此完全沒被檢查，而測試名字宣稱檢查了全部。
    """
    delegated = _delegated_calls()
    bad = [lineno for lineno, kw in _record_calls()
           if lineno not in delegated and _reason_pdf_pairs(kw) is None]
    assert not bad, (
        f'這些呼叫點的 reason/pdf 形狀攤不開，守門測試看不進去：{bad}。'
        '請寫成常數或 `X if cond else Y`，不要讓規則躲在運算式裡')


def test_unknown_reasons_never_pass_a_numeric_count():
    """未取得那一類的 reason 只能寫 None。

    這一條防的是「以後有人覺得補個 0 比較整齊」。它會真的碰撞：把任何一個
    呼叫點改成 `pdf=0` 就立刻紅（條件運算式那兩個也算在內）。

    執行期還有 _record_source_scan 自己擋（見上面兩條 ValueError 測試）；
    這裡是靜態那一道，連沒有測試覆蓋到的路徑也能在跑到之前就擋下來。
    """
    offenders = []
    delegated = _delegated_calls()
    for lineno, kw in _record_calls():
        if lineno in delegated:
            continue
        for reason, pdf in _reason_pdf_pairs(kw) or []:
            unknown = reason in _UNKNOWN_REASONS
            is_none = isinstance(pdf, ast.Constant) and pdf.value is None
            if unknown != is_none:
                offenders.append((lineno, reason,
                                  ast.dump(pdf) if pdf is not None else None))
    assert not offenders, (
        f'這些呼叫點的 reason 與 pdf 不一致（未取得≠0，量到的也不可寫 None）：'
        f'{offenders}')


def test_measured_and_unknown_are_actually_different_values():
    """最終防線用行為斷言，不只看原始碼：兩種結果必須真的不同。"""
    empty = PermitSync._scan_result({'skipped': 0, 'failed': 0}, [], [])
    failed = PermitSync._scan_result({'skipped': 0, 'failed': 1}, [], [])
    assert empty['reason'] == 'empty' and empty['pdf'] == 0
    assert failed['reason'] == 'list_failed' and failed['pdf'] is None
    assert empty != failed, '兩種成因收斂成同一筆資料的話，這整組測試就失去意義'


@pytest.mark.parametrize('failed', [0, 1])
@pytest.mark.parametrize('n_pdf', [0, 2])
@pytest.mark.parametrize('n_other', [0, 3])
@pytest.mark.parametrize('skipped', [0, 1])
def test_scan_result_is_consistent_for_every_combination(failed, n_pdf, n_other,
                                                         skipped, monkeypatch):
    """窮舉 `_scan_result()` 的輸入組合：每一種都要滿足落檔入口的不變式。

    `sync_permit` 的兩個呼叫點是 `**_scan_result(...)` 展開的，靜態掃描看不進去。
    與其放過，不如把那個純函式的所有組合跑一遍，再真的送進 `_record_source_scan()`
    ——它自己會對 reason／pdf 不一致拋 ValueError。16 種組合全部不得拋。
    """
    scan = {'skipped': skipped, 'failed': failed}
    pdfs = [('i', 'a.pdf', '', '', 'application/pdf')] * n_pdf
    others = [('i', 'a.xls', '', '', 'application/vnd.ms-excel')] * n_other
    kwargs = PermitSync._scan_result(scan, pdfs, others)

    if failed:
        assert kwargs['pdf'] is None, '清單不完整時份數只是下限，不可寫成定論'
        assert kwargs['reason'] in ('partial', 'list_failed')
    else:
        assert kwargs['pdf'] == n_pdf, 'pdf 只能算 PDF，不含鏡像的非 PDF'
        expected = 'ok' if n_pdf else ('no_pdf_but_other'
                                       if (n_other or skipped) else 'empty')
        assert kwargs['reason'] == expected
    assert kwargs['other'] == n_other
    assert kwargs['skipped'] == skipped

    s = _sync(monkeypatch, [{'files': []}])
    s._record_source_scan('A建字第0001號', **kwargs)      # 不得拋 ValueError
    assert _scan_entry(s)['reason'] == kwargs['reason']


# ───────────── 不變式擋在落檔入口，不靠呼叫點自律（#114 review 後補） ─────────────
#
# 原本「未取得只能寫 None」只有一支掃原始碼的守門測試在看，而六個呼叫點裡有兩個
# 的 reason 是條件運算式（`'partial' if ... else 'ok'`）——AST 掃到的是 IfExp，
# 不是常數，等於那兩個呼叫點沒人守。所以把規則搬進 _record_source_scan 自己。

def test_entry_point_rejects_unknown_reason_with_a_count(monkeypatch):
    s = _sync(monkeypatch, [{'files': []}])
    with pytest.raises(ValueError, match='list_failed'):
        s._record_source_scan('A建字第0001號', pdf=0, other=0, reason='list_failed')
    assert _scan_entry(s) is None, '擋下來的呼叫不可留下半筆資料'


def test_entry_point_rejects_measured_reason_without_a_count(monkeypatch):
    """反方向也要擋：reason 說量到了、份數卻是 None，讀的人無從判斷。"""
    s = _sync(monkeypatch, [{'files': []}])
    with pytest.raises(ValueError, match='empty'):
        s._record_source_scan('A建字第0001號', pdf=None, other=0, reason='empty')


def test_other_none_stays_none(monkeypatch):
    """`other=None`＝非 PDF 檔數沒量到。寫成 0 就是宣稱「看過、沒有」。"""
    s = _sync(monkeypatch, [{'files': []}])
    s._record_source_scan('A建字第0001號', pdf=3, other=None, reason='ok')
    assert _scan_entry(s)['other'] is None
    s._record_source_scan('A建字第0001號', pdf=3, other=0, reason='ok')
    assert _scan_entry(s)['other'] == 0, '兩者必須是不同的值，否則白分'


# ───────────── review P1：清單不完整必須進連續失敗結算 ─────────────
#
# partial 原本只寫了 pdf=None 就結束：該案已被 _mark_visited，卻不在
# _errored_permits，於是 finalize_run 把它當成「這輪成功」並**清掉既有 streak**。
# 同一個子資料夾天天 403，每輪都留下 partial，卻永遠到不了三輪告警——資料層知道
# 本輪不完整、連續失敗層得到相反結論，和 #111 修掉的是同一個錯誤類型。

def _partial_sync(monkeypatch):
    s = _sync(monkeypatch, [
        {'files': [_pdf('p1'), _folder('sub')]},
        HttpError(_FakeResp(403), b'denied'),    # 子資料夾無權限（持久失敗）
    ])
    monkeypatch.setattr(s, 'preload_target_files', lambda *a, **k: None)
    monkeypatch.setattr(s, 'check_file_exists', lambda *a, **k: True)
    return s


def test_partial_listing_is_recorded_as_an_error(monkeypatch):
    s = _partial_sync(monkeypatch)
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    assert _scan_entry(s)['reason'] == 'partial'
    assert 'A建字第0001號' in s._errored_permits, (
        '沒進 _errored_permits → finalize_run 會把它當成功並清掉 streak')
    errs = [e for e in s.state.get('errors', []) if e['permit'] == 'A建字第0001號']
    assert len(errs) == 1 and errs[0].get('at'), '一輪一筆、而且要帶時間戳'
    assert '資料夾' in errs[0]['error']


def test_partial_listing_increments_streak_instead_of_resetting_it(monkeypatch):
    """P1 真正的傷害在這裡：已經累積兩輪的案不可被判成恢復。"""
    from geobingan_sync import sync_errors

    s = _partial_sync(monkeypatch)
    s.state['error_streak'] = {'A建字第0001號': 2}
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')

    res = sync_errors.finalize_run(s.state, s._visited_permits, s._errored_permits,
                                   now=datetime(2026, 10, 12, 10, 0, 0))
    assert s.state['error_streak']['A建字第0001號'] == 3, '應累積到 3 而不是歸零'
    assert res['newly_stuck'] == ['A建字第0001號'], '第三輪要跨過門檻並告警'
    assert res['recovered'] == [], '清單不完整的案不可被報成恢復'


def test_fully_listed_permit_still_counts_as_a_clean_round(monkeypatch):
    """反面對照：沒有列出失敗的案仍要被判成成功，否則上面那條無論怎麼寫都會過。"""
    from geobingan_sync import sync_errors

    s = _sync(monkeypatch, [{'files': [_pdf('p1')]}])
    monkeypatch.setattr(s, 'preload_target_files', lambda *a, **k: None)
    monkeypatch.setattr(s, 'check_file_exists', lambda *a, **k: True)
    s.state['error_streak'] = {'A建字第0001號': 2}
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')

    res = sync_errors.finalize_run(s.state, s._visited_permits, s._errored_permits,
                                   now=datetime(2026, 10, 12, 10, 0, 0))
    assert 'A建字第0001號' not in s.state['error_streak']
    assert res['recovered'] == ['A建字第0001號']


def test_list_failure_records_exactly_one_error(monkeypatch):
    """錯誤改成在列檔後一次記完，空清單那條不可變成記兩筆。"""
    s = _sync(monkeypatch, [HttpError(_FakeResp(403), b'denied')])
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    assert len([e for e in s.state['errors'] if e['permit'] == 'A建字第0001號']) == 1


# ───────────── review P2：adapter 兩條路徑也要落檔 ─────────────
#
# sync_permit() 遇到 adapter 就提前 return，所以逐檔與整包兩條路一份量測都沒寫。
# 文件寫的是「每案每輪把來源到底有什麼寫成資料」，正式環境已有 adapter 在跑的案
# 卻在 source_scan 裡呈現為從未量測——帳上和「這案根本沒走到」無法區分。

def _adapter_sync(monkeypatch):
    s = _sync(monkeypatch, [])
    monkeypatch.setattr(s, 'preload_target_files', lambda *a, **k: None)
    monkeypatch.setattr(s, 'check_file_exists', lambda *a, **k: False)
    monkeypatch.setattr(s, 'upload_bytes', lambda *a, **k: 'fid')
    monkeypatch.setattr(s, 'upload_remote_file', lambda *a, **k: ('fid', 'tgt'))
    return s


def _src(name):
    from geobingan_sync.source_adapters.base import SourceFile
    return SourceFile(name=name, url=f'https://fake.test/{name}')


class _ListAdapter:
    """沒有 fetch_all → 走逐檔路徑。"""
    name = 'fake-list'

    def __init__(self, files=(), boom=False):
        self._files, self._boom = list(files), boom

    def matches(self, url):
        return True

    def list_files(self, url):
        from geobingan_sync.source_adapters import AdapterError
        if self._boom:
            raise AdapterError('HTTPError:403')
        return list(self._files)


class _BundleAdapter:
    """有 fetch_all → 走整包路徑。`boom_after=n`：吐出 n 份後炸掉。"""
    name = 'fake-bundle'

    def __init__(self, members=(), boom_after=None):
        self._members, self._boom_after = list(members), boom_after

    def matches(self, url):
        return True

    def fetch_all(self, url):
        from geobingan_sync.source_adapters import AdapterError
        for i, m in enumerate(self._members):
            if self._boom_after is not None and i >= self._boom_after:
                raise AdapterError('ReadTimeout:整包下載逾時')
            yield m, b'%PDF-1.4\n'
        if self._boom_after is not None and self._boom_after >= len(self._members):
            raise AdapterError('ReadTimeout:整包下載逾時')


def test_adapter_per_file_records_the_count(monkeypatch):
    s = _adapter_sync(monkeypatch)
    s._sync_via_adapter('A建字第0001號', 'https://fake.test/f',
                        'tgt', _ListAdapter([_src('a.pdf'), _src('b.pdf')]))
    e = _scan_entry(s)
    assert e['reason'] == 'ok'
    assert e['pdf'] == 2
    assert e['other'] is None, 'adapter 列檔時就把非 PDF 丟了，那個數字沒量到'


def test_adapter_per_file_empty_is_a_measured_zero(monkeypatch):
    s = _adapter_sync(monkeypatch)
    s._sync_via_adapter('A建字第0001號', 'https://fake.test/f', 'tgt', _ListAdapter([]))
    e = _scan_entry(s)
    assert e['reason'] == 'empty'
    assert e['pdf'] == 0, '列完了、來源真的沒有 PDF：這是實測 0'


def test_adapter_list_failure_is_unknown_not_zero(monkeypatch):
    s = _adapter_sync(monkeypatch)
    s._sync_via_adapter('A建字第0001號', 'https://fake.test/f', 'tgt',
                        _ListAdapter(boom=True))
    e = _scan_entry(s)
    assert e['reason'] == 'list_failed'
    assert e['pdf'] is None
    assert 'A建字第0001號' in s._errored_permits, '原本就有記錯誤，不可因為這次改動掉了'


def test_adapter_download_failures_do_not_erase_the_measured_count(monkeypatch):
    """逐檔取檔失敗是**同步**問題，不是量測問題。來源清單已完整取得，份數仍是定論。"""
    from geobingan_sync.source_adapters import AdapterError

    s = _adapter_sync(monkeypatch)

    def _boom(*a, **k):
        raise AdapterError('取檔逾時')

    monkeypatch.setattr(s, 'upload_remote_file', _boom)
    s._sync_via_adapter('A建字第0001號', 'https://fake.test/f',
                        'tgt', _ListAdapter([_src('a.pdf'), _src('b.pdf')]))
    e = _scan_entry(s)
    assert e['reason'] == 'ok' and e['pdf'] == 2, '來源有 2 份這件事沒有變'
    assert 'A建字第0001號' in s._errored_permits, '但取檔失敗仍要進連續失敗結算'


def test_bundle_records_the_member_count(monkeypatch):
    s = _adapter_sync(monkeypatch)
    s._sync_via_bundle('A建字第0001號', 'https://fake.test/z', 'tgt',
                       _BundleAdapter([_src('a.pdf'), _src('b.pdf'), _src('c.pdf')]))
    e = _scan_entry(s)
    assert e['reason'] == 'ok'
    assert e['pdf'] == 3


def test_bundle_yielding_nothing_is_not_a_measured_zero(monkeypatch):
    """整包來源有取用間隔（dropbox 每 7 天一次），未到間隔時 fetch_all 什麼都不吐。

    那不是錯誤、也不是「來源沒有 PDF」。從同步端看不出「刻意跳過」與「整包真的
    是空的」的差別，所以只能記成未取得——寫成實測 0 的話，dropbox 的案七天裡有
    六天都會在帳上宣稱來源是空的。
    """
    s = _adapter_sync(monkeypatch)
    s._sync_via_bundle('A建字第0001號', 'https://fake.test/z', 'tgt', _BundleAdapter([]))
    e = _scan_entry(s)
    assert e['pdf'] is None, '沒吐出任何成員不等於來源沒有 PDF'
    assert e['reason'] == 'not_measured'
    assert 'A建字第0001號' not in s._errored_permits, '刻意跳過不是錯誤，不可累積 streak'


def test_bundle_failure_midway_is_partial_not_a_count(monkeypatch):
    """炸在第幾個成員決定證據等級：已吐出 2 份才斷掉，2 是下限不是份數。"""
    s = _adapter_sync(monkeypatch)
    s._sync_via_bundle('A建字第0001號', 'https://fake.test/z', 'tgt',
                       _BundleAdapter([_src('a.pdf'), _src('b.pdf'), _src('c.pdf')],
                                      boom_after=2))
    e = _scan_entry(s)
    assert e['reason'] == 'partial'
    assert e['pdf'] is None
    assert 'A建字第0001號' in s._errored_permits


def test_bundle_failure_at_the_start_is_list_failed(monkeypatch):
    s = _adapter_sync(monkeypatch)
    s._sync_via_bundle('A建字第0001號', 'https://fake.test/z', 'tgt',
                       _BundleAdapter([_src('a.pdf')], boom_after=0))
    e = _scan_entry(s)
    assert e['reason'] == 'list_failed'
    assert e['pdf'] is None


def test_sync_permit_routes_to_adapter_and_still_records(monkeypatch):
    """從真正的進入點進去：sync_permit 遇到 adapter 提前 return 的那條路。

    直接呼叫 _sync_via_adapter 驗不到這件事——P2 的症狀正是「分支在上游就岔開，
    量測從此不會被執行」。
    """
    import geobingan_sync.source_adapters as sa

    s = _adapter_sync(monkeypatch)
    adapter = _ListAdapter([_src('a.pdf')])
    # sync_permit 是在函式內 `from ... import find_adapter`，所以要換掉**模組**上的
    # 那個名字；換 sync_permits 的同名屬性沒有任何作用（它從來不讀那裡）。
    monkeypatch.setattr(sa, 'find_adapter', lambda url: adapter)
    s.sync_permit('A建字第0001號', 'https://fake.test/f', 'tgt')
    e = _scan_entry(s)
    assert e is not None, 'adapter 路徑不可完全不落檔'
    assert e['reason'] == 'ok' and e['pdf'] == 1
    assert 'A建字第0001號' in s._visited_permits


# ───────── 資料夾不見了 ≠ 資料夾是空的（2026-10-14） ─────────
#
# 列檔用的查詢是 `'<id>' in parents`，資料夾被刪掉之後它**不會報錯**，只是回零
# 筆。於是 14 個建照（來源 URL 正是 registry 列管的失效連結）被記成 `empty`、
# `pdf: 0`，我再把那個 0 讀成「承造人的資料夾開著但裡面沒東西」發了出去。
# 這幾條釘住：零筆的時候要先問資料夾還在不在，而且只有 404／trashed 才算不在。

def test_a_deleted_source_folder_is_not_measured_zero(monkeypatch):
    gone = HttpError(_FakeResp(404, 'notFound'), b'{}')
    s = _sync(monkeypatch, [{'files': []}], folder_state=gone)
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    e = _scan_entry(s)
    assert e['reason'] == 'source_gone', '資料夾 404 要記成「來源不見了」'
    assert e['pdf'] is None, '量不到就只能是 None；寫 0 就是把失效講成「有在收但是 0 份」'


def test_a_trashed_source_folder_is_not_measured_zero(monkeypatch):
    s = _sync(monkeypatch, [{'files': []}], folder_state='trashed')
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    e = _scan_entry(s)
    assert e['reason'] == 'source_gone', '丟進垃圾桶也是不見了'
    assert e['pdf'] is None


@pytest.mark.parametrize('status', [403, 500, 503])
def test_unprovable_states_are_unknown_not_gone(monkeypatch, status):
    """403 是權限被收回、5xx 是對方的問題——都不足以斷定「不存在」。

    斷言失效會讓這案進失效名單、被拿去找承造人；寧可記未取得，下一輪再確認。
    """
    err = HttpError(_FakeResp(status, 'nope'), b'{}')
    s = _sync(monkeypatch, [{'files': []}], folder_state=err)
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    e = _scan_entry(s)
    assert e['reason'] == 'list_failed', f'{status} 不可以被講成 source_gone'
    assert e['pdf'] is None
    assert 'A建字第0001號' in s._errored_permits, (
        '查不到狀態是真的失敗，要進錯誤帳才會有連續失敗告警')


def test_an_empty_but_living_folder_is_still_measured_zero(monkeypatch):
    """反面：資料夾還在、真的沒東西，仍然要是實測 0（不可一併改成未取得）。"""
    s = _sync(monkeypatch, [{'files': []}], folder_state='alive')
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    e = _scan_entry(s)
    assert (e['reason'], e['pdf']) == ('empty', 0)


def test_the_folder_probe_only_runs_when_nothing_was_listed(monkeypatch):
    """有檔案就證明資料夾在，不必多打一次 API（400 案逐案多打一次不划算）。"""
    s = _sync(monkeypatch, [{'files': [_pdf('a')]}])
    monkeypatch.setattr(s, 'preload_target_files', lambda *a, **k: None)
    monkeypatch.setattr(s, 'check_file_exists', lambda *a, **k: True)
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    assert _scan_entry(s)['reason'] == 'ok'
    assert s._get_svc().files().get_calls == [], '列到檔案時不應該再探測資料夾'


def test_skipped_types_also_prove_the_folder_is_there(monkeypatch):
    """只有不收的型別（.doc／.zip）也算證明資料夾在，同樣不必探測。"""
    s = _sync(monkeypatch, [{'files': [
        {'id': 'd', 'name': '施工計畫書.doc', 'mimeType': 'application/msword'}]}])
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    assert _scan_entry(s)['reason'] == 'no_pdf_but_other'
    assert s._get_svc().files().get_calls == []


def test_source_gone_shows_up_in_the_run_summary(monkeypatch, capsys):
    """寫成資料但沒人看，和只印在日誌一樣是空窗（同 no_pdf_but_other 那條）。"""
    s = _sync(monkeypatch, [{'files': []}])
    s._visited_permits = {'A建字第0001號'}
    s.state['source_scan'] = {
        'A建字第0001號': {'at': 'x', 'reason': 'source_gone', 'pdf': None, 'other': None},
    }
    s._finalize_errors(completed=True)
    out = capsys.readouterr().out
    assert '來源資料夾已不存在' in out
    assert 'A建字第0001號' in out


def test_source_gone_is_registered_as_an_unknown_reason():
    """守門：`source_gone` 必須在未取得那一類，否則 `_record_source_scan` 會
    允許它帶著數字落檔，實測 0 與量不到又混回去。"""
    assert 'source_gone' in sp_mod.UNKNOWN_SCAN_REASONS
