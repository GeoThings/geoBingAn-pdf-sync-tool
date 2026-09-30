"""清單指紋：內容變更偵測、靜態退回告警、停更偵測（批次 B1）。"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.list_fingerprint import (compute_digest, diff_permits, assess,
                                             format_change, ListFingerprint)
import health_check

T0 = datetime(2026, 9, 15, 10, 0)


def test_digest_is_order_and_dup_insensitive():
    assert compute_digest(['b', 'a']) == compute_digest(['a', 'b', 'a'])
    assert compute_digest(['a']) != compute_digest(['a', 'b'])


def test_diff_permits():
    added, removed = diff_permits(['a', 'b'], ['b', 'c'])
    assert added == ['c'] and removed == ['a']


def test_first_update_is_baseline_not_change(tmp_path):
    fp = ListFingerprint(tmp_path / 'f.json')
    changed, summary, state = fp.update('表單回復_1150902.pdf', '動態', ['a', 'b'], now=T0)
    assert changed is False and summary is None      # 首次建立基線不通知
    assert state['permit_count'] == 2 and state['source'] == '動態'


def test_content_change_reports_added_and_removed(tmp_path):
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('舊.pdf', '動態', ['a', 'b'], now=T0)
    changed, summary, state = fp.update('表單回復_1151002.pdf', '動態', ['b', 'c', 'd'],
                                        now=T0 + timedelta(days=30))
    assert changed is True
    assert '新增 2 筆' in summary and '移除 1 筆' in summary
    assert state['last_changed'] == (T0 + timedelta(days=30)).isoformat()


def test_unchanged_keeps_last_changed(tmp_path):
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('x.pdf', '動態', ['a'], now=T0)
    changed, summary, state = fp.update('x.pdf', '動態', ['a'], now=T0 + timedelta(days=5))
    assert changed is False and summary is None
    assert state['last_changed'] == T0.isoformat()          # 只更新 last_checked
    assert state['last_checked'] == (T0 + timedelta(days=5)).isoformat()


def test_assess_static_fallback_is_error(tmp_path):
    """核心缺口：動態解析失效退回靜態＝可能同步過期清單，必須是 error。"""
    fp = ListFingerprint(tmp_path / 'f.json')
    _, _, state = fp.update('03b35db7-....pdf', '靜態', ['a'], now=T0)
    level, msg = assess(state, now=T0)
    assert level == 'error' and '靜態備援' in msg


def test_assess_stale_list_is_warning(tmp_path):
    fp = ListFingerprint(tmp_path / 'f.json')
    _, _, state = fp.update('x.pdf', '動態', ['a'], now=T0)
    assert assess(state, now=T0 + timedelta(days=59))[0] == 'ok'
    level, msg = assess(state, now=T0 + timedelta(days=61))
    assert level == 'warning' and '疑似停更' in msg


def test_assess_no_baseline_is_ok():
    assert assess({})[0] == 'ok'
    assert assess({'label': 'x'})[0] == 'ok'


def test_health_check_wrapper_reads_state(tmp_path):
    path = tmp_path / 'f.json'
    ListFingerprint(path).update('表單回復_1150902.pdf', '動態', ['a', 'b'], now=T0)
    level, msg = health_check.check_list_freshness(path=path, now=T0)
    assert level == 'ok' and '表單回復_1150902.pdf' in msg and '2 筆' in msg


def test_format_change_handles_only_added():
    msg = format_change('x.pdf', '動態', ['a', 'b'], [], 10)
    assert '新增 2 筆' in msg and '移除' not in msg


# ---------- review P2：通知送達才清 pending ----------

def test_change_queues_pending_notice(tmp_path):
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('舊.pdf', '動態', ['a'], now=T0)
    _, summary, state = fp.update('新.pdf', '動態', ['a', 'b'], now=T0 + timedelta(days=1))
    assert state['pending_notices'] == [summary]


def test_pending_survives_until_cleared_and_accumulates(tmp_path):
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', ['a'], now=T0)
    fp.update('v2.pdf', '動態', ['a', 'b'], now=T0 + timedelta(days=1))      # 變更 1（未送達）
    _, _, state = fp.update('v2.pdf', '動態', ['a', 'b'], now=T0 + timedelta(days=2))
    assert len(state['pending_notices']) == 1        # 無變更不重複排入
    _, _, state = fp.update('v3.pdf', '動態', ['a', 'b', 'c'], now=T0 + timedelta(days=3))
    assert len(state['pending_notices']) == 2        # 第二次變更累積，不覆蓋掉前一則
    state = fp.clear_pending(now=T0 + timedelta(days=3))
    assert state['pending_notices'] == []
    # 清除後不會再冒出來
    _, _, state = fp.update('v3.pdf', '動態', ['a', 'b', 'c'], now=T0 + timedelta(days=4))
    assert state['pending_notices'] == []


def test_state_stays_fresh_even_while_notice_pending(tmp_path):
    """通知未送達也要更新 source/label，否則健康檢查會讀到過期的來源資訊。"""
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', ['a'], now=T0)
    _, _, state = fp.update('v2.pdf', '靜態', ['a', 'b'], now=T0 + timedelta(days=1))
    assert state['source'] == '靜態' and state['label'] == 'v2.pdf'
    assert assess(state, now=T0 + timedelta(days=1))[0] == 'error'   # 退回靜態仍照常告警


def test_sync_clears_pending_only_when_delivered(tmp_path, monkeypatch):
    import geobingan_sync.steps.sync_permits as sp
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', ['a'], now=T0)
    fp.update('v2.pdf', '動態', ['a', 'b'], now=T0 + timedelta(days=1))
    assert fp.load()['pending_notices']

    monkeypatch.setattr(sp.PermitSync, '_send_list_change_notice', staticmethod(lambda n: False))
    ps = sp.PermitSync.__new__(sp.PermitSync)
    ps.list_label, ps.list_source = 'v2.pdf', '動態'
    ps.permit_mapping = {'a': '', 'b': ''}
    # 指紋基準改為「PDF 裡的建照號」，測試物件要提供該欄位（production 由
    # parse_pdf_list 設定）。不提供就 AttributeError，那是刻意的：缺欄位要大聲壞掉。
    ps.permits_in_list = ['a', 'b']
    monkeypatch.setattr(sp, 'ListFingerprint', lambda *a, **k: fp, raising=False)
    import geobingan_sync.list_fingerprint as lf
    monkeypatch.setattr(lf, 'ListFingerprint', lambda *a, **k: fp)
    ps._record_list_fingerprint()
    assert fp.load()['pending_notices'], '未送達應保留待重試'

    monkeypatch.setattr(sp.PermitSync, '_send_list_change_notice', staticmethod(lambda n: True))
    ps._record_list_fingerprint()
    assert fp.load()['pending_notices'] == [], '送達後應清除'


# ---------- review P1：指紋寫入失敗必須 fail-closed ----------

def test_fingerprint_write_failure_aborts_sync(tmp_path, monkeypatch):
    """退回靜態時若指紋寫不進去，不可吞掉繼續——否則 health_check 仍讀到舊的『動態』。"""
    import geobingan_sync.steps.sync_permits as sp
    import geobingan_sync.list_fingerprint as lf

    path = tmp_path / 'f.json'
    ListFingerprint(path).update('v1.pdf', '動態', ['a'], now=T0)     # 上輪：動態

    fp = ListFingerprint(path)
    monkeypatch.setattr(fp, 'save', lambda data: (_ for _ in ()).throw(OSError('disk full')))
    monkeypatch.setattr(lf, 'ListFingerprint', lambda *a, **k: fp)

    ps = sp.PermitSync.__new__(sp.PermitSync)
    ps.list_label, ps.list_source = 'legacy.pdf', '靜態'              # 本輪退回靜態
    ps.permit_mapping = {'a': '', 'b': ''}
    # 指紋基準改為「PDF 裡的建照號」，測試物件要提供該欄位（production 由
    # parse_pdf_list 設定）。不提供就 AttributeError，那是刻意的：缺欄位要大聲壞掉。
    ps.permits_in_list = ['a', 'b']
    try:
        ps._record_list_fingerprint()
        assert False, '應該要 raise，讓同步步驟失敗'
    except OSError:
        pass
    # 狀態未被更新（仍是上輪的動態），但因為同步已中止，不會拿可能過期的清單繼續
    assert ListFingerprint(path).load()['source'] == '動態'


def test_notice_failure_does_not_abort_sync(tmp_path, monkeypatch):
    """相對地：通知失敗只保留 pending，不能讓同步失敗。"""
    import geobingan_sync.steps.sync_permits as sp
    import geobingan_sync.list_fingerprint as lf

    path = tmp_path / 'f.json'
    fp = ListFingerprint(path)
    fp.update('v1.pdf', '動態', ['a'], now=T0)
    monkeypatch.setattr(lf, 'ListFingerprint', lambda *a, **k: fp)
    monkeypatch.setattr(sp.PermitSync, '_send_list_change_notice',
                        staticmethod(lambda n: (_ for _ in ()).throw(RuntimeError('clickup down'))))
    ps = sp.PermitSync.__new__(sp.PermitSync)
    ps.list_label, ps.list_source = 'v2.pdf', '動態'
    ps.permit_mapping = {'a': '', 'b': ''}
    # 指紋基準改為「PDF 裡的建照號」，測試物件要提供該欄位（production 由
    # parse_pdf_list 設定）。不提供就 AttributeError，那是刻意的：缺欄位要大聲壞掉。
    ps.permits_in_list = ['a', 'b']
    ps._record_list_fingerprint()                       # 不應拋出
    assert fp.load()['pending_notices'], '通知未送達應保留待重試'
    assert fp.load()['label'] == 'v2.pdf'               # 指紋本身照常更新


# ---------- 指紋只能反映對方的清單，不可混入我方解析能力（2026-09-30）----------
#
# 原本取「解析出連結的建照」，於是 PR #99 讓解析改抓任何 http(s) 之後，集合從 368
# 變 440、指紋跟著變，機制發出「清單已更新，新增 72 筆」——政府一個字都沒改，
# 新增的 72 筆全是我們原本丟掉的非 Drive 連結（實測比對確認）。
#
# 後果不只假通知：last_changed 被推到當天，而「疑似停更」靠它判定，等於我們自己
# 把停更偵測的時鐘重置了。反方向更糟——解析退化漏掉建照會報「移除 N 筆」，
# 看起來像政府下架建案。

from geobingan_sync.list_fingerprint import (BASIS_LEGACY_WITH_LINKS, BASIS_PERMITS_IN_PDF)

LISTED = ['110建字第0001號', '110建字第0002號', '110建字第0003號']


def test_digest_ignores_our_link_coverage(tmp_path):
    """同一份清單，我方解析出的連結數變了，指紋不可變。"""
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', LISTED, now=T0, permits_with_links=1)
    changed, summary, state = fp.update('v1.pdf', '動態', LISTED,
                                        now=T0 + timedelta(days=1), permits_with_links=3)
    assert changed is False and summary is None, '解析涵蓋率變動不是清單更新'
    assert state['last_changed'] == T0.isoformat(), '不可推進停更時鐘'
    assert state['permits_with_links'] == 3, '涵蓋率仍要記錄，只是不進指紋'


def test_real_list_change_is_still_detected(tmp_path):
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', LISTED, now=T0)
    changed, summary, state = fp.update('v2.pdf', '動態', LISTED + ['111建字第0009號'],
                                        now=T0 + timedelta(days=1))
    assert changed is True and '新增 1 筆' in summary
    assert state['last_changed'] == (T0 + timedelta(days=1)).isoformat()


def test_removed_permits_still_detected(tmp_path):
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', LISTED, now=T0)
    changed, summary, _ = fp.update('v2.pdf', '動態', LISTED[:-1], now=T0 + timedelta(days=1))
    assert changed is True and '移除 1 筆' in summary


# ---------- 基準遷移 ----------

def _legacy_state(tmp_path, permits, now):
    """寫一份舊格式（沒有 basis 欄位）的指紋檔。"""
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', permits, now=now, basis=BASIS_LEGACY_WITH_LINKS)
    data = fp.load()
    data.pop('basis', None)          # 舊檔真的沒有這個欄位
    fp.save(data)
    return fp


def test_basis_migration_is_not_reported_as_a_list_change(tmp_path):
    """切換基準時指紋必然改變，但那不是對方改了清單。"""
    fp = _legacy_state(tmp_path, ['110建字第0001號'], T0)          # 舊：只有 1 筆有連結
    changed, summary, state = fp.update('v1.pdf', '動態', LISTED,  # 新：清單其實有 3 筆
                                        now=T0 + timedelta(days=10))
    assert changed is False and summary is None
    assert state['pending_notices'] == [], '遷移不可排入變更通知'
    assert state['basis'] == BASIS_PERMITS_IN_PDF
    assert state['basis_migrated_from'] == BASIS_LEGACY_WITH_LINKS
    assert state['basis_migrated_at'] == (T0 + timedelta(days=10)).isoformat()


def test_basis_migration_does_not_reset_the_stale_clock(tmp_path):
    """最關鍵的一條：遷移不可把 last_changed 推到今天，否則停更偵測歸零。"""
    fp = _legacy_state(tmp_path, ['110建字第0001號'], T0)
    _c, _s, state = fp.update('v1.pdf', '動態', LISTED, now=T0 + timedelta(days=50))
    assert state['last_changed'] == T0.isoformat()
    level, msg = assess(state, now=T0 + timedelta(days=61))
    assert level == 'warning' and '疑似停更' in msg, '遷移後仍要看得出清單很久沒變'


def test_change_after_migration_is_detected_normally(tmp_path):
    fp = _legacy_state(tmp_path, ['110建字第0001號'], T0)
    fp.update('v1.pdf', '動態', LISTED, now=T0 + timedelta(days=1))          # 遷移
    changed, summary, state = fp.update('v2.pdf', '動態', LISTED + ['111建字第0009號'],
                                        now=T0 + timedelta(days=2))
    assert changed is True and '新增 1 筆' in summary
    assert state['last_changed'] == (T0 + timedelta(days=2)).isoformat()


def test_migration_happens_only_once(tmp_path):
    fp = _legacy_state(tmp_path, ['110建字第0001號'], T0)
    fp.update('v1.pdf', '動態', LISTED, now=T0 + timedelta(days=1))
    _c, _s, state = fp.update('v1.pdf', '動態', LISTED, now=T0 + timedelta(days=2))
    assert state.get('basis_migrated_at') is None or \
        state['basis_migrated_at'] == (T0 + timedelta(days=1)).isoformat()


def test_first_baseline_records_basis(tmp_path):
    fp = ListFingerprint(tmp_path / 'f.json')
    changed, _s, state = fp.update('v1.pdf', '動態', LISTED, now=T0)
    assert changed is False and state['basis'] == BASIS_PERMITS_IN_PDF
    assert 'basis_migrated_at' not in state, '首次建立不是遷移'


# ---------- 管線：指紋必須拿到「PDF 裡的建照」而不是「有連結的建照」 ----------

def test_parse_collects_permits_without_links(tmp_path, monkeypatch):
    """沒有連結的建照也要進 permits_in_list，否則指紋又會跟著解析能力跑。"""
    import geobingan_sync.steps.sync_permits as sp

    class _Page:
        def extract_text(self):
            return ('110建字第0001號 甲 乙 https://drive.google.com/drive/folders/'
                    '1AbCdEfGhIjKlMnOpQrStUvWxYz012345 中文 '
                    '110建字第0002號 丙 丁 尚未提供 '
                    '110建字第0003號 戊 己 中文尚未提供\n1 / 1')

    class _Reader:
        def __init__(self, f):
            self.pages = [_Page()]
    monkeypatch.setattr(sp.pypdf, 'PdfReader', _Reader)
    f = tmp_path / 'l.pdf'
    f.write_bytes(b'%PDF-1.4')
    ps = sp.PermitSync(city={'name': 'T', 'pdf_list_url': 'https://x.test/l.pdf'})
    mapping = ps.parse_pdf_list(str(f))
    assert set(mapping) == {'110建字第0001號'}, '只有第一個有連結'
    assert ps.permits_in_list == ['110建字第0001號', '110建字第0002號', '110建字第0003號'], \
        '三個建照都在清單裡，指紋要用這個'


def test_fingerprint_call_uses_permits_in_list_not_mapping():
    """守門：若改回用 permit_mapping.keys()，指紋又會跟著我方解析能力跑。

    只掃**程式碼行**。解釋這件事的註解裡本來就會提到 permit_mapping.keys()，
    掃原始碼的守門測試第三次被自己的註解打中了——範圍要縮到真正在意的東西。
    """
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'geobingan_sync/steps/sync_permits.py')
    src = open(path, encoding='utf-8').read()
    body = src[src.index('def _record_list_fingerprint'):]
    body = body[:body.index('\n    def ', 10)]
    code = '\n'.join(ln for ln in body.split('\n') if not ln.lstrip().startswith('#'))
    call = code[code.index('fp.update('):]
    assert 'self.permits_in_list' in call, '指紋必須用「PDF 裡的建照」當基準'
    assert 'permit_mapping.keys()' not in call, '指紋不可用「有連結的建照」當基準'


# ---------- review P1：解析全掛時必須 fail-closed ----------
#
# 已有 440 筆基線卻一筆都抽不到（PDF 文字抽取或建照正則失效），原本會照寫下去：
# ①誤報「移除 440 筆」並排入通知 ②把基線覆蓋成空的，下一輪再誤報「新增 440 筆」
# ③把 last_changed 推到當天、重設停更時鐘。三個後果都比「同步這一步失敗」嚴重。

from geobingan_sync.list_fingerprint import EmptyPermitSet

BASE440 = [f'110建字第{i:04d}號' for i in range(1, 441)]


def test_empty_permits_with_existing_baseline_raises(tmp_path):
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', BASE440, now=T0)
    with pytest.raises(EmptyPermitSet, match='440 筆'):
        fp.update('v1.pdf', '動態', [], now=T0 + timedelta(days=5))


def test_baseline_survives_a_failed_parse(tmp_path):
    """最關鍵：擋下來之後基線必須完好，否則下一輪會誤報「新增 440 筆」。"""
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', BASE440, now=T0)
    with pytest.raises(EmptyPermitSet):
        fp.update('v1.pdf', '動態', [], now=T0 + timedelta(days=5))
    st = fp.load()
    assert st['permit_count'] == 440 and len(st['permits']) == 440
    assert st['last_changed'] == T0.isoformat(), '不可推進停更時鐘'
    assert st['pending_notices'] == [], '不可排入假的移除通知'


def test_recovery_after_a_failed_parse_is_not_a_change(tmp_path):
    """解析修好後再跑，因為基線沒被毀，不該報任何變更。"""
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', BASE440, now=T0)
    with pytest.raises(EmptyPermitSet):
        fp.update('v1.pdf', '動態', [], now=T0 + timedelta(days=5))
    changed, summary, st = fp.update('v1.pdf', '動態', BASE440, now=T0 + timedelta(days=6))
    assert changed is False and summary is None
    assert st['last_changed'] == T0.isoformat()


def test_empty_permits_without_baseline_is_allowed(tmp_path):
    """還沒有基線時寫空的沒有破壞性（首次建立），不要因此擋住。"""
    fp = ListFingerprint(tmp_path / 'f.json')
    changed, summary, st = fp.update('v1.pdf', '動態', [], now=T0)
    assert changed is False and st['permit_count'] == 0


def test_genuine_removals_are_still_reported(tmp_path):
    """守門不可誤擋真實的移除：只有「全空」才算解析失效。"""
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', BASE440, now=T0)
    changed, summary, _ = fp.update('v2.pdf', '動態', BASE440[:100], now=T0 + timedelta(days=1))
    assert changed is True and '移除 340 筆' in summary


def test_sync_step_propagates_empty_parse_failure(tmp_path, monkeypatch):
    """管線層：例外要往上傳，讓同步這一步失敗（shell 才會告警）。"""
    import geobingan_sync.steps.sync_permits as sp
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', BASE440, now=T0)
    monkeypatch.setattr(sp, 'ListFingerprint', lambda *a, **k: fp, raising=False)
    import geobingan_sync.list_fingerprint as lf
    monkeypatch.setattr(lf, 'ListFingerprint', lambda *a, **k: fp)
    ps = sp.PermitSync.__new__(sp.PermitSync)
    ps.list_label, ps.list_source = 'v1.pdf', '動態'
    ps.permit_mapping = {}
    ps.permits_in_list = []
    with pytest.raises(EmptyPermitSet):
        ps._record_list_fingerprint()
    assert fp.load()['permit_count'] == 440, '基線必須完好'


# ---------- review P2：遷移不可送出舊基準留下的待送通知 ----------

def test_migration_discards_stale_pending_notices(tmp_path):
    """舊基準的待送通知無法在新基準下驗證（9/30 那則假通知正是這樣產生的）。"""
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', ['A'], now=T0, basis=BASIS_LEGACY_WITH_LINKS)
    fp.update('v2.pdf', '動態', ['A', 'B'], now=T0 + timedelta(days=1),
              basis=BASIS_LEGACY_WITH_LINKS)
    assert fp.load()['pending_notices'], '前提：遷移前有待送通知'
    data = fp.load(); data.pop('basis', None); fp.save(data)

    changed, summary, st = fp.update('v2.pdf', '動態', ['A', 'B', 'C'],
                                     now=T0 + timedelta(days=2))
    assert changed is False and summary is None
    assert st['pending_notices'] == [], '舊基準的通知不可留在佇列被送出'
    assert len(st['basis_migration_discarded_notices']) == 1, '丟棄要留紀錄，不可靜默'
    assert '新增 1 筆' in st['basis_migration_discarded_notices'][0]


def test_migration_without_pending_records_nothing_extra(tmp_path):
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', ['A'], now=T0, basis=BASIS_LEGACY_WITH_LINKS)
    data = fp.load(); data.pop('basis', None); data['pending_notices'] = []; fp.save(data)
    _c, _s, st = fp.update('v1.pdf', '動態', ['A', 'B'], now=T0 + timedelta(days=1))
    assert 'basis_migration_discarded_notices' not in st


def test_pending_after_migration_still_works_normally(tmp_path):
    """遷移之後的真實變更仍要正常排入待送佇列。"""
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', ['A'], now=T0, basis=BASIS_LEGACY_WITH_LINKS)
    data = fp.load(); data.pop('basis', None); fp.save(data)
    fp.update('v1.pdf', '動態', ['A', 'B'], now=T0 + timedelta(days=1))      # 遷移
    _c, _s, st = fp.update('v2.pdf', '動態', ['A', 'B', 'C'], now=T0 + timedelta(days=2))
    assert len(st['pending_notices']) == 1 and '新增 1 筆' in st['pending_notices'][0]


def test_sync_step_announces_discarded_notices(tmp_path, monkeypatch, capsys):
    """丟棄不可靜默——操作者要看得到丟了什麼。"""
    import geobingan_sync.steps.sync_permits as sp
    fp = ListFingerprint(tmp_path / 'f.json')
    fp.update('v1.pdf', '動態', ['A'], now=T0, basis=BASIS_LEGACY_WITH_LINKS)
    fp.update('v2.pdf', '動態', ['A', 'B'], now=T0 + timedelta(days=1),
              basis=BASIS_LEGACY_WITH_LINKS)
    data = fp.load(); data.pop('basis', None); fp.save(data)

    monkeypatch.setattr(sp.PermitSync, '_send_list_change_notice', staticmethod(lambda n: True))
    monkeypatch.setattr(sp, 'ListFingerprint', lambda *a, **k: fp, raising=False)
    import geobingan_sync.list_fingerprint as lf
    monkeypatch.setattr(lf, 'ListFingerprint', lambda *a, **k: fp)
    ps = sp.PermitSync.__new__(sp.PermitSync)
    ps.list_label, ps.list_source = 'v2.pdf', '動態'
    ps.permit_mapping = {'A': '', 'B': '', 'C': ''}
    ps.permits_in_list = ['A', 'B', 'C']
    ps._record_list_fingerprint()
    out = capsys.readouterr().out
    assert '指紋基準已從' in out and '丟棄舊基準留下的 1 則' in out
