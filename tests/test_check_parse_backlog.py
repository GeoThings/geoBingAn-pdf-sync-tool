"""health_check：解析積壓（B2）與解析預算檢查。"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import health_check
from geobingan_sync.budget import MonthlyBudget

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _r(hours_ago, status='pending'):
    return {'parse_status': status, 'updated_at': (NOW - timedelta(hours=hours_ago)).isoformat()}


def test_count_stale_reports_pure():
    pages = [[_r(30), _r(7), _r(1), _r(24 * 30)], [_r(0.5, 'processing'), {'parse_status': 'pending'}]]
    c = health_check.count_stale_reports(pages, NOW)
    assert c['total'] == 6 and c['recent'] == 2 and c['ancient'] == 1 and round(c['recent_oldest_h']) == 30


def test_ancient_backlog_alone_is_ok_but_noted(monkeypatch):
    """陳年積壓不驅動燈號（否則舊帳讓告警永遠紅、蓋掉新事故），但要註記。"""
    _patch_api(monkeypatch, [[_r(24 * 200) for _ in range(50)]])
    level, msg = health_check.check_parse_backlog(now=NOW)
    assert level == 'ok' and '陳年積壓 50 份' in msg


def test_new_incident_on_top_of_ancient_backlog_is_error(monkeypatch):
    _patch_api(monkeypatch, [[_r(24 * 200) for _ in range(50)] + [_r(25) for _ in range(116)]])
    level, msg = health_check.check_parse_backlog(now=NOW)
    assert level == 'error' and '116 份近期解析停滯' in msg and '陳年積壓 50 份' in msg


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self): return self._p


def _patch_api(monkeypatch, pending_pages, processing_pages=None):
    calls = {'n': 0}
    processing_pages = processing_pages or [[]]
    def fake_get(url, headers=None, timeout=None):
        calls['n'] += 1
        if 'parse_status=pending' in url or url.startswith('next-pending'):
            pages = pending_pages
        else:
            pages = processing_pages
        idx = int(url.split('#')[1]) if '#' in url else 0
        nxt = f"{'next-pending' if pages is pending_pages else 'next-processing'}#{idx+1}" if idx + 1 < len(pages) else None
        return _Resp({'results': pages[idx], 'next': nxt})
    monkeypatch.setattr(health_check, '_api_token', lambda: 'tok')
    import requests
    monkeypatch.setattr(requests, 'get', fake_get)
    return calls


def test_backlog_error_when_many_stale(monkeypatch):
    stale = [_r(25) for _ in range(116)]
    _patch_api(monkeypatch, [stale[:100], stale[100:]])
    level, msg = health_check.check_parse_backlog(now=NOW)
    assert level == 'error' and '116 份近期解析停滯' in msg


def test_backlog_warning_when_few_stale(monkeypatch):
    _patch_api(monkeypatch, [[_r(7), _r(1)]])
    level, msg = health_check.check_parse_backlog(now=NOW)
    assert level == 'warning' and '1 份近期解析停滯' in msg


def test_backlog_ok_when_all_fresh(monkeypatch):
    _patch_api(monkeypatch, [[_r(1), _r(2)]])
    assert health_check.check_parse_backlog(now=NOW)[0] == 'ok'


def test_backlog_api_failure_is_warning(monkeypatch):
    monkeypatch.setattr(health_check, '_api_token', lambda: (_ for _ in ()).throw(RuntimeError('auth down')))
    level, msg = health_check.check_parse_backlog(now=NOW)
    assert level == 'warning' and 'auth down' in msg


def test_check_budget_levels(monkeypatch, tmp_path):
    import geobingan_sync.budget as b
    path = tmp_path / 'b.json'
    monkeypatch.setattr(b.MonthlyBudget, '__init__',
                        lambda self, path=None, cost_per_report=0.3: (setattr(self, 'path', tmp_path / 'b.json'), setattr(self, 'cost_per_report', cost_per_report)) and None)
    monkeypatch.setattr('geobingan_sync.config.MONTHLY_BUDGET_USD', 100.0)
    b.MonthlyBudget().add(310, now=datetime.now())          # 310×0.3 = 93 → 93%
    level, msg = health_check.check_budget()
    assert level == 'error' and '93%' in msg


def _patch_static(monkeypatch, resp):
    monkeypatch.setattr(health_check, '_api_token', lambda: 'tok')
    import requests
    monkeypatch.setattr(requests, 'get', lambda url, headers=None, timeout=None: resp)


def test_http_401_json_is_warning_not_false_green(monkeypatch):
    """review P2：401 的 JSON 不可被當成佇列正常。"""
    _patch_static(monkeypatch, _Resp({'detail': 'Authentication credentials were not provided.'}, status=401))
    level, msg = health_check.check_parse_backlog(now=NOW)
    assert level == 'warning' and 'HTTP 401' in msg


def test_http_500_is_warning(monkeypatch):
    _patch_static(monkeypatch, _Resp({'error': 'boom'}, status=500))
    assert health_check.check_parse_backlog(now=NOW)[0] == 'warning'


def test_http_200_malformed_payload_is_warning(monkeypatch):
    _patch_static(monkeypatch, _Resp({'detail': 'weird'}, status=200))
    level, msg = health_check.check_parse_backlog(now=NOW)
    assert level == 'warning' and '格式異常' in msg
