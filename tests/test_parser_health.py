"""解析引擎健康探測（上傳前閘門）。

2026-09-21：OpenAI 帳戶餘額用光，4 份 failed、後端標成 invalid_json；6 份撞應用層
閘門。這些都不會自動恢復。探測要在上傳前就攔住「送進去必卡」的情況，但不能
把「只是撞閘門」誤判成故障（那是正常的額度用完，午夜會重置）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.parser_health import assess, classify_error, hold_if_unhealthy, Verdict

NOW = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)


def _r(status, created_h_ago=1.0, parsed_h_ago=None, error=None, skip=None):
    m = {}
    if error: m['parse_error'] = error
    if skip: m['skip_reason'] = skip
    if parsed_h_ago is not None: m['parsed_at'] = (NOW - timedelta(hours=parsed_h_ago)).isoformat()
    return {'id': f'{status}-{created_h_ago}', 'parse_status': status,
            'created_at': (NOW - timedelta(hours=created_h_ago)).isoformat(), 'metadata': m}


BILLING = "Failed to parse PDF after trying all models (['gpt-5.5', 'gpt-5.4']). Last error: You have no credits remaining. Add credits to continue using the API"
QUOTA = 'Data Foundry daily estimated-cost budget is exhausted.'
DETERMINISTIC = '文件輸出達長度上限，請將文件分頁後重試。'


@pytest.mark.parametrize('text,kind', [
    (BILLING, 'billing'), (QUOTA, 'quota'), (DETERMINISTIC, 'deterministic'),
    ('Invalid JSON from Gemini', 'other'), (None, None), ('', None),
])
def test_classify_error(text, kind):
    assert classify_error(text) == kind


def test_billing_failure_holds():
    """帳戶沒餘額 → 擋下。後端把它標 invalid_json，所以只能看原文。"""
    v = assess([_r('completed', 3, parsed_h_ago=2.5), _r('failed', 2, error=BILLING)], NOW)
    assert not v.ok and 'no credits' in v.reason and v.stats['billing'] == 1


def test_quota_only_is_ok():
    """只撞應用層閘門是正常的額度用完，午夜重置；不擋。"""
    v = assess([_r('completed', 3, parsed_h_ago=2.5), _r('pending', 8, error=QUOTA)], NOW)
    assert v.ok and v.stats['quota'] == 1 and v.stats['stalled'] == 0


def test_stalled_queue_holds():
    """pending 超過 6h、期間零完成、又不是 quota → worker 停擺 → 擋。"""
    v = assess([_r('pending', 7), _r('pending', 9), _r('completed', 20, parsed_h_ago=19)], NOW)
    assert not v.ok and 'stalled' not in v.reason and '停擺' in v.reason and v.stats['stalled'] == 2


def test_old_pending_but_recent_completion_is_ok():
    """有舊 pending 但最近有完成 → worker 活著，只是排隊；不擋。"""
    v = assess([_r('pending', 7), _r('completed', 1, parsed_h_ago=0.5)], NOW)
    assert v.ok


def test_fresh_pending_is_ok():
    """剛上傳還沒輪到（<6h）不算停擺。"""
    v = assess([_r('pending', 1), _r('pending', 2)], NOW)
    assert v.ok


def test_empty_window_assess_alone_is_ok():
    """assess() 只看視窗，空的就沒話說；真正的判斷在 evaluate（要合併上次狀態）。"""
    assert assess([], NOW).ok


# ---------- 沒有證據 ≠ 健康（2026-09-22 實例） ----------

def test_empty_window_carries_over_previous_hold():
    """前一天 billing 失敗滑出 24h 視窗後，探測不得因為「看不到」而放行。

    實際發生：9/21 11:00 上傳的 4 份 billing 失敗，9/22 12:00 已超過 24h，
    探測印「解析引擎正常（completed 0、pending 0、quota 擋 0）」並放行 15 份。
    """
    from geobingan_sync.parser_health import evaluate
    prev = {'status': 'held', 'reason': '近 24h 有 4 份因 OpenAI 帳戶無餘額失敗',
            'kind': 'billing', 'last_bad': BAD_AT}
    v, st = evaluate([], NOW, prev)
    assert not v.ok and '沒有恢復證據' in v.reason
    assert st['status'] == 'held'                       # 狀態保留，不被清掉


BAD_AT = (NOW - timedelta(hours=4)).isoformat()      # 事故發生在 4 小時前
HELD = {'status': 'held', 'reason': '帳戶無餘額', 'kind': 'billing', 'last_bad': BAD_AT}


def test_completed_after_incident_clears_hold():
    """解除要看見「事故之後」的完成。"""
    from geobingan_sync.parser_health import evaluate
    v, st = evaluate([_r('completed', 1, parsed_h_ago=0.5)], NOW, HELD)     # 0.5h 前完成 > 4h 前事故
    assert v.ok and '已恢復' in v.reason and st['status'] == 'ok'


def test_completed_before_incident_does_not_clear_hold():
    """P1：事故**之前**的完成不算恢復證據。

    視窗是滑動的 24h，事故當天稍早成功的那些也還在視窗內；拿它們開閘
    等於用事故前的資料證明事故已解決。
    """
    from geobingan_sync.parser_health import evaluate
    v, st = evaluate([_r('completed', 8, parsed_h_ago=7)], NOW, HELD)       # 7h 前完成 < 4h 前事故
    assert not v.ok and '事故之前' in v.reason
    assert st['status'] == 'held' and v.stats['recovered_after_bad'] == 0


def test_mixed_completions_need_one_after_incident():
    from geobingan_sync.parser_health import evaluate
    before = _r('completed', 8, parsed_h_ago=7)
    after = _r('completed', 1, parsed_h_ago=0.5)
    assert not evaluate([before], NOW, HELD)[0].ok
    assert evaluate([before, after], NOW, HELD)[0].ok


def test_hold_without_timestamp_stays_held():
    """舊狀態檔沒記事故時間 → 保守不解除，交給 canary。"""
    from geobingan_sync.parser_health import evaluate
    v, _ = evaluate([_r('completed', 1, parsed_h_ago=0.5)], NOW, {'status': 'held', 'reason': 'x'})
    assert not v.ok


def test_bad_observation_records_hold():
    from geobingan_sync.parser_health import evaluate
    v, st = evaluate([_r('failed', 2, error=BILLING)], NOW, {})
    assert not v.ok and st['status'] == 'held' and st['kind'] == 'billing'


def test_empty_window_without_previous_hold_is_ok():
    """沒有前科時，空視窗不該無故擋下（否則靜默一陣子就再也傳不了）。"""
    from geobingan_sync.parser_health import evaluate
    v, st = evaluate([], NOW, {})
    assert v.ok and st.get('status') != 'held'


def test_state_roundtrip(tmp_path):
    from geobingan_sync.parser_health import load_state, save_state
    p = str(tmp_path / 's.json')
    assert load_state(p) == {}
    save_state({'status': 'held', 'kind': 'billing'}, p)
    assert load_state(p)['kind'] == 'billing'


def test_probe_fetches_canary_by_id_outside_window(monkeypatch, tmp_path):
    """P1：canary 重推的是幾天前建立的報告，24h created_at 視窗抓不到——必須按 id 補抓。

    不補抓的話 canary 成功也解不開 held，等於唯一出口是死的。
    """
    import geobingan_sync.parser_health as ph
    p = str(tmp_path / 's.json')
    bad = (NOW - timedelta(hours=30)).isoformat()
    ph.save_state({'status': 'held', 'kind': 'billing', 'reason': '帳戶無餘額',
                   'last_bad': bad, 'canary_ids': ['old-1']}, p)
    canary_done = {'id': 'old-1', 'parse_status': 'completed',
                   'created_at': (NOW - timedelta(days=5)).isoformat(),
                   'metadata': {'parsed_at': (NOW - timedelta(minutes=20)).isoformat()}}
    monkeypatch.setattr(ph, 'fetch_recent', lambda *a, **k: [])          # 視窗內什麼都沒有
    monkeypatch.setattr(ph, 'fetch_by_ids', lambda base, h, ids, get=None: [canary_done])
    monkeypatch.setattr('geobingan_sync.steps.upload_pdfs._get_valid_token', lambda: 'tok')
    monkeypatch.setattr('geobingan_sync.steps.drain_stuck.load_our_names', lambda: set())
    v = ph.probe(now=NOW, state_path=p)
    assert v.ok and '已恢復' in v.reason
    assert ph.load_state(p)['status'] == 'ok'


def test_probe_canary_still_pending_keeps_hold(monkeypatch, tmp_path):
    import geobingan_sync.parser_health as ph
    p = str(tmp_path / 's.json')
    ph.save_state({'status': 'held', 'kind': 'billing', 'reason': 'x',
                   'last_bad': (NOW - timedelta(hours=30)).isoformat(), 'canary_ids': ['old-1']}, p)
    still = {'id': 'old-1', 'parse_status': 'pending',
             'created_at': (NOW - timedelta(days=5)).isoformat(), 'metadata': {}}
    monkeypatch.setattr(ph, 'fetch_recent', lambda *a, **k: [])
    monkeypatch.setattr(ph, 'fetch_by_ids', lambda base, h, ids, get=None: [still])
    monkeypatch.setattr('geobingan_sync.steps.upload_pdfs._get_valid_token', lambda: 'tok')
    monkeypatch.setattr('geobingan_sync.steps.drain_stuck.load_our_names', lambda: set())
    assert not ph.probe(now=NOW, state_path=p).ok


def test_fetch_by_ids_skips_failures():
    from geobingan_sync.parser_health import fetch_by_ids
    class R:
        def __init__(s, p): s._p = p
        def raise_for_status(s): pass
        def json(s): return s._p
    def get(url, headers=None, timeout=None):
        if url.rstrip('/').endswith('bad'): raise OSError('down')
        return R({'id': 'good'})
    assert [r['id'] for r in fetch_by_ids('http://x', {}, ['good', 'bad'], get=get)] == ['good']


def test_probe_failure_does_not_clear_hold(monkeypatch, tmp_path):
    """探測本身失敗＝沒有新資訊，不可把先前的 held 寫掉。"""
    import geobingan_sync.parser_health as ph
    p = str(tmp_path / 's.json')
    ph.save_state({'status': 'held', 'kind': 'billing', 'reason': 'x'}, p)
    monkeypatch.setattr(ph, 'fetch_recent', lambda *a, **k: (_ for _ in ()).throw(OSError('down')))
    monkeypatch.setattr('geobingan_sync.steps.upload_pdfs._get_valid_token', lambda: 'tok')
    v = ph.probe(now=NOW, state_path=p)
    assert not v.ok and ph.load_state(p)['status'] == 'held' 


def test_deterministic_and_other_failures_do_not_hold():
    """輸出超上限那類是單檔問題，不代表引擎壞；一般解析失敗也不擋（會被 drain 重試）。"""
    v = assess([_r('failed', 2, error=DETERMINISTIC), _r('failed', 3, error='Invalid JSON'),
                _r('completed', 1, parsed_h_ago=0.5)], NOW)
    assert v.ok and v.stats['deterministic'] == 1 and v.stats['other_error'] == 1


def test_hold_exits_with_dedicated_code_when_unhealthy(capsys):
    from geobingan_sync.parser_health import EXIT_PARSER_HELD
    with pytest.raises(SystemExit) as e:
        hold_if_unhealthy(skip=False, probe_fn=lambda: Verdict(False, '帳戶沒餘額'))
    assert e.value.code == EXIT_PARSER_HELD == 5      # 4 留給真正的異常，不可重載
    assert '暫停上傳' in capsys.readouterr().out


def test_hold_skip_only_warns(capsys):
    v = hold_if_unhealthy(skip=True, probe_fn=lambda: Verdict(False, '帳戶沒餘額'))
    assert not v.ok and '照常上傳' in capsys.readouterr().out


def test_hold_passes_when_healthy():
    assert hold_if_unhealthy(probe_fn=lambda: Verdict(True, 'ok')).ok


def test_no_detail_pending_is_not_counted_as_stalled():
    """抓不到 detail 的 pending 分不出 quota／停擺 → 不算 stalled（review P2：cap 超過後誤判）。"""
    r = _r('pending', 9); r['_no_detail'] = True
    v = assess([r, _r('pending', 9)], NOW)              # 第二個有 detail、無錯誤 → 才算停擺
    assert v.stats['stalled'] == 1 and v.stats['no_detail'] == 1


class _Resp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p


def test_fetch_recent_filters_to_our_reports_and_details_all_noncompleted():
    """探測必須真的只看我方（review P2），且非 completed 全抓 detail。"""
    from geobingan_sync.parser_health import fetch_recent
    from geobingan_sync.steps.drain_stuck import norm_name
    listing = [
        {'id': '1', 'file_name': 'ours.pdf', 'parse_status': 'pending', 'created_at': (NOW - timedelta(hours=1)).isoformat()},
        {'id': '2', 'file_name': 'theirs.pdf', 'parse_status': 'failed', 'created_at': (NOW - timedelta(hours=1)).isoformat()},
        {'id': '3', 'file_name': 'ours2', 'parse_status': 'completed', 'created_at': (NOW - timedelta(hours=2)).isoformat()},
    ]
    calls = []
    def get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        if url.endswith('/construction-reports/'):
            return _Resp({'results': listing, 'next': None})
        rid = url.rstrip('/').split('/')[-1]
        row = next(x for x in listing if x['id'] == rid)
        return _Resp(dict(row, metadata={'parse_error': 'budget is exhausted'} if rid == '1' else {}))
    ours = {norm_name('ours.pdf'), norm_name('ours2.pdf')}
    out = fetch_recent('http://x', {}, NOW, get=get, our_names=ours)
    assert [r['id'] for r in out] == ['1', '3']                       # theirs 被濾掉
    assert out[0]['metadata']['parse_error'] == 'budget is exhausted'  # 非 completed 抓了 detail
    assert not any(url.endswith('/2/') for url in calls)              # 別人的連 detail 都不抓


def test_fetch_recent_marks_no_detail_beyond_cap():
    from geobingan_sync.parser_health import fetch_recent
    listing = [{'id': str(i), 'file_name': f'{i}.pdf', 'parse_status': 'pending',
                'created_at': (NOW - timedelta(hours=1)).isoformat()} for i in range(5)]
    def get(url, headers=None, params=None, timeout=None):
        if url.endswith('/construction-reports/'):
            return _Resp({'results': listing, 'next': None})
        return _Resp(dict(next(x for x in listing if url.rstrip('/').endswith('/' + x['id'])), metadata={}))
    out = fetch_recent('http://x', {}, NOW, get=get, detail_cap=2)
    assert sum(1 for r in out if r.get('_no_detail')) == 3
    v = assess(out, NOW)
    assert v.stats['no_detail'] == 3 and v.stats['stalled'] == 0        # 剛上傳 1h，本來就不 stalled；且無 detail 的不計


def test_probe_error_is_not_health(monkeypatch):
    """探測本身失敗＝狀態未知 → 不放行（fail-closed），不能當成健康。"""
    import geobingan_sync.parser_health as ph
    monkeypatch.setattr(ph, 'fetch_recent', lambda *a, **k: (_ for _ in ()).throw(OSError('down')))
    monkeypatch.setattr('geobingan_sync.steps.upload_pdfs._get_valid_token', lambda: 'tok')
    v = ph.probe(now=NOW)
    assert not v.ok and '未知' in v.reason
