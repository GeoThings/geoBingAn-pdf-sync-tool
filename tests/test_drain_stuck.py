"""隔日自動放行：只挑我方、近期、pending/failed、排除確定性失敗；先探健康再送。"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.parser_health import Verdict
from geobingan_sync.steps import drain_stuck as ds

NOW = datetime(2026, 9, 22, 0, 20, tzinfo=timezone.utc)


def _r(rid, name, status, days_ago=1, error=None):
    return {'id': rid, 'file_name': name, 'parse_status': status,
            'created_at': (NOW - timedelta(days=days_ago)).isoformat(),
            'metadata': {'parse_error': error} if error else {}}


OURS = {ds.norm_name(n) for n in ('a.pdf', 'b.pdf', 'c.pdf', 'd.pdf', 'old.pdf')}


def test_select_only_ours_recent_stuck_and_not_deterministic():
    reports = [
        _r('1', 'a.pdf', 'pending'),
        _r('2', 'b.pdf', 'failed', error='You have no credits remaining'),
        _r('3', 'c.pdf', 'failed', error='文件輸出達長度上限，請將文件分頁後重試。'),   # 確定性 → 不重試
        _r('4', 'd.pdf', 'completed'),                                                     # 已完成
        _r('5', 'someone-else.pdf', 'pending'),                                            # 不是我方
        _r('6', 'old.pdf', 'pending', days_ago=10),                                        # 太舊
    ]
    ids, skipped = ds.select_stuck(reports, OURS, NOW, days=7)
    assert ids == ['1', '2']
    assert [s[0] for s in skipped] == ['3']


def test_load_our_names_strips_folder_and_extension(tmp_path):
    p = tmp_path / 'h.json'
    p.write_text('{"uploaded_files": ["資料夾A/x.pdf", "資料夾B/y.PDF", "資料夾C/沒副檔名"]}', encoding='utf-8')
    assert ds.load_our_names(str(p)) == {'x', 'y', '沒副檔名'}


def test_match_is_extension_insensitive():
    """真實案例：歷史記「兩廳院(基地)(1150907~1150912)NO.72」（無副檔名），後端是「….pdf」。"""
    names = {ds.norm_name('兩廳院(基地)(1150907~1150912)NO.72')}
    reports = [_r('1', '兩廳院(基地)(1150907~1150912)NO.72.pdf', 'pending')]
    ids, _ = ds.select_stuck(reports, names, NOW, days=7)
    assert ids == ['1']


def test_main_refuses_when_parser_unhealthy():
    called = []
    rc = ds.main(probe_fn=lambda: Verdict(False, '帳戶沒餘額'), fetch_fn=lambda: [], retry_fn=lambda i: called.append(i) or 0)
    assert rc == 4 and called == []                       # 不送：retry-parse 端點不查預算，送了只會再被擋


def test_main_skip_health_still_sends():
    called = []
    rc = ds.main(probe_fn=lambda: Verdict(False, 'x'), skip_parser_health=True,
                 fetch_fn=lambda: [_r('1', 'a.pdf', 'pending')], our_names=OURS, now=NOW,
                 retry_fn=lambda i: called.append(i) or 0)
    assert rc == 0 and called == [['1']]


def test_main_caps_and_delegates_to_retry():
    reports = [_r(str(i), 'a.pdf', 'pending') for i in range(30)]
    called = []
    rc = ds.main(max_items=20, probe_fn=lambda: Verdict(True, 'ok'), fetch_fn=lambda: reports,
                 our_names=OURS, now=NOW, retry_fn=lambda i: called.append(i) or 0)
    assert rc == 0 and len(called[0]) == 20


def test_main_nothing_to_do():
    rc = ds.main(probe_fn=lambda: Verdict(True, 'ok'), fetch_fn=lambda: [], our_names=OURS, now=NOW,
                 retry_fn=lambda i: (_ for _ in ()).throw(AssertionError('不該呼叫')))
    assert rc == 0
