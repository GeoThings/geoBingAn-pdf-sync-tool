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


def test_empty_is_ok():
    assert assess([], NOW).ok


def test_deterministic_and_other_failures_do_not_hold():
    """輸出超上限那類是單檔問題，不代表引擎壞；一般解析失敗也不擋（會被 drain 重試）。"""
    v = assess([_r('failed', 2, error=DETERMINISTIC), _r('failed', 3, error='Invalid JSON'),
                _r('completed', 1, parsed_h_ago=0.5)], NOW)
    assert v.ok and v.stats['deterministic'] == 1 and v.stats['other_error'] == 1


def test_hold_exits_4_when_unhealthy(capsys):
    with pytest.raises(SystemExit) as e:
        hold_if_unhealthy(skip=False, probe_fn=lambda: Verdict(False, '帳戶沒餘額'))
    assert e.value.code == 4
    assert '暫停上傳' in capsys.readouterr().out


def test_hold_skip_only_warns(capsys):
    v = hold_if_unhealthy(skip=True, probe_fn=lambda: Verdict(False, '帳戶沒餘額'))
    assert not v.ok and '照常上傳' in capsys.readouterr().out


def test_hold_passes_when_healthy():
    assert hold_if_unhealthy(probe_fn=lambda: Verdict(True, 'ok')).ok


def test_probe_error_is_not_health(monkeypatch):
    """探測本身失敗＝狀態未知 → 不放行（fail-closed），不能當成健康。"""
    import geobingan_sync.parser_health as ph
    monkeypatch.setattr(ph, 'fetch_recent', lambda *a, **k: (_ for _ in ()).throw(OSError('down')))
    monkeypatch.setattr('geobingan_sync.steps.upload_pdfs._get_valid_token', lambda: 'tok')
    v = ph.probe(now=NOW)
    assert not v.ok and '未知' in v.reason
