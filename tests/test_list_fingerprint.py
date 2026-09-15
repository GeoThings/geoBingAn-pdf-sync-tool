"""清單指紋：內容變更偵測、靜態退回告警、停更偵測（批次 B1）。"""
import os
import sys
from datetime import datetime, timedelta

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
    monkeypatch.setattr(sp, 'ListFingerprint', lambda *a, **k: fp, raising=False)
    import geobingan_sync.list_fingerprint as lf
    monkeypatch.setattr(lf, 'ListFingerprint', lambda *a, **k: fp)
    ps._record_list_fingerprint()
    assert fp.load()['pending_notices'], '未送達應保留待重試'

    monkeypatch.setattr(sp.PermitSync, '_send_list_change_notice', staticmethod(lambda n: True))
    ps._record_list_fingerprint()
    assert fp.load()['pending_notices'] == [], '送達後應清除'
