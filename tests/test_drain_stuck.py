"""隔日自動放行：只挑我方、近期、pending/failed、排除確定性失敗；先探健康再送。"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.parser_health import Verdict
from geobingan_sync.parser_health import load_state  # noqa: F401
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


def test_unhealthy_sends_one_canary_then_refuses_same_day(tmp_path):
    """held 只能靠「看見恢復」解除；沒人送東西就永遠看不見 → 每天送 1 份 canary 探路。"""
    sp = str(tmp_path / 's.json')
    sent = []
    def ok_retry(i, stats=None):
        sent.append(i)
        if stats is not None: stats.update({'accepted': len(i)})
        return 0
    rc = ds.main(probe_fn=lambda: Verdict(False, '帳戶沒餘額'), state_path=sp, now=NOW,
                 fetch_fn=lambda: [_r(str(i), 'a.pdf', 'pending') for i in range(5)],
                 our_names=OURS, retry_fn=ok_retry)
    assert rc == 0 and len(sent) == 1 and len(sent[0]) == 1        # 只送 1 份
    assert ds.load_state(sp)['canary_ids'] == sent[0]              # 記下 id 供探測按 id 補抓

    rc2 = ds.main(probe_fn=lambda: Verdict(False, '帳戶沒餘額'), state_path=sp, now=NOW,
                  fetch_fn=lambda: [_r('9', 'a.pdf', 'pending')], our_names=OURS,
                  retry_fn=ok_retry)
    assert rc2 == 5 and len(sent) == 1                             # 同日不再送第二隻


def test_canary_day_not_burned_when_no_candidate(tmp_path):
    """P2：沒有候選可送時不可記入今日額度——held 的唯一出口是 canary。"""
    sp = str(tmp_path / 's.json')
    rc = ds.main(probe_fn=lambda: Verdict(False, 'x'), state_path=sp, now=NOW,
                 fetch_fn=lambda: [], our_names=OURS, retry_fn=lambda i, stats=None: 0)
    assert rc == 0 and 'canary_day' not in ds.load_state(sp)
    sent = []
    rc2 = ds.main(probe_fn=lambda: Verdict(False, 'x'), state_path=sp, now=NOW,
                  fetch_fn=lambda: [_r('1', 'a.pdf', 'pending')], our_names=OURS,
                  retry_fn=lambda i, stats=None: (sent.append(i), stats.update({'accepted': 1}) if stats is not None else None, 0)[-1])
    assert rc2 == 0 and len(sent) == 1                             # 隔一次仍可探路


def test_canary_day_not_burned_when_retry_fails(tmp_path):
    """P2：retry 失敗（例如預算擋下）也不可記入今日額度。"""
    sp = str(tmp_path / 's.json')
    rc = ds.main(probe_fn=lambda: Verdict(False, 'x'), state_path=sp, now=NOW,
                 fetch_fn=lambda: [_r('1', 'a.pdf', 'pending')], our_names=OURS,
                 retry_fn=_retry({}, rc=3))
    assert rc == 3 and 'canary_day' not in ds.load_state(sp)


def _retry(stats_payload, rc=0):
    def fn(i, stats=None):
        if stats is not None:
            stats.update(stats_payload)
        return rc
    return fn


def test_canary_day_not_burned_when_all_rejected(tmp_path):
    """全被 4xx 明確拒絕＝確定沒消耗 → 當日仍可再探。"""
    sp = str(tmp_path / 's.json')
    rc = ds.main(probe_fn=lambda: Verdict(False, 'x'), state_path=sp, now=NOW,
                 fetch_fn=lambda: [_r('1', 'a.pdf', 'pending')], our_names=OURS,
                 retry_fn=_retry({'accepted': 0, 'rejected': 1, 'unknown': 0}))
    assert rc == 0
    st = ds.load_state(sp)
    assert 'canary_day' not in st and 'canary_ids' not in st


def test_canary_unknown_counts_as_consumed(tmp_path):
    """P2：逾時／5xx＝結果不明，後端可能已受理 → 保守視為用掉，且保留 id 供後續查。

    與 budget.ReservationLedger 同一個原則：只有確定零成本才退還。
    否則同日重跑會再送一次、重複解析。
    """
    sp = str(tmp_path / 's.json')
    sent = []
    def fn(i, stats=None):
        sent.append(i)
        if stats is not None: stats.update({'accepted': 0, 'rejected': 0, 'unknown': 1})
        return 0
    rc = ds.main(probe_fn=lambda: Verdict(False, 'x'), state_path=sp, now=NOW,
                 fetch_fn=lambda: [_r('1', 'a.pdf', 'pending')], our_names=OURS, retry_fn=fn)
    assert rc == 0
    st = ds.load_state(sp)
    assert st['canary_day'] == NOW.strftime('%Y-%m-%d') and st['canary_ids'] == ['1']

    rc2 = ds.main(probe_fn=lambda: Verdict(False, 'x'), state_path=sp, now=NOW,
                  fetch_fn=lambda: [_r('2', 'a.pdf', 'pending')], our_names=OURS, retry_fn=fn)
    assert rc2 == 5 and len(sent) == 1                    # 同日不再送，避免重複解析


def test_canary_consumed_even_if_rc_nonzero_when_sent(tmp_path):
    """送出去了但整批回 4（有查詢失敗）仍算消耗——送出與否看 stats，不看 exit code。"""
    sp = str(tmp_path / 's.json')
    rc = ds.main(probe_fn=lambda: Verdict(False, 'x'), state_path=sp, now=NOW,
                 fetch_fn=lambda: [_r('1', 'a.pdf', 'pending')], our_names=OURS,
                 retry_fn=_retry({'accepted': 1, 'rejected': 0, 'unknown': 0}, rc=4))
    assert rc == 4 and ds.load_state(sp)['canary_day'] == NOW.strftime('%Y-%m-%d')


def test_canary_day_recorded_only_on_success(tmp_path):
    sp = str(tmp_path / 's.json')
    ds.main(probe_fn=lambda: Verdict(False, 'x'), state_path=sp, now=NOW,
            fetch_fn=lambda: [_r('1', 'a.pdf', 'pending')], our_names=OURS,
            retry_fn=lambda i, stats=None: (stats.update({'accepted': 1}) if stats is not None else None, 0)[-1])
    st = ds.load_state(sp)
    assert st['canary_day'] == NOW.strftime('%Y-%m-%d') and st.get('canary_sent_at')
    assert st['canary_ids'] == ['1']


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

# ---------- 結束碼語意不可重載（review P1） ----------

def test_deliberate_hold_uses_dedicated_exit_code(tmp_path):
    """有意不放行 → EXIT_PARSER_HELD（5），不是 4。"""
    from geobingan_sync.parser_health import EXIT_PARSER_HELD
    sp = str(tmp_path / 's.json')
    ds.save_state({'canary_day': NOW.strftime('%Y-%m-%d')}, sp)
    rc = ds.main(probe_fn=lambda: Verdict(False, '帳戶沒餘額'), state_path=sp, now=NOW,
                 fetch_fn=lambda: [], our_names=OURS, retry_fn=_retry({}))
    assert rc == EXIT_PARSER_HELD == 5


def test_real_query_failure_still_exits_4(tmp_path):
    """retry_parse 查詢失敗／狀態未知仍回 4——必須被巡檢當成異常告警，不可被吞掉。"""
    sp = str(tmp_path / 's.json')
    rc = ds.main(probe_fn=lambda: Verdict(True, 'ok'), state_path=sp, now=NOW,
                 fetch_fn=lambda: [_r('1', 'a.pdf', 'pending')], our_names=OURS,
                 retry_fn=_retry({'accepted': 0, 'rejected': 0, 'unknown': 0}, rc=4))
    assert rc == 4


# ---------- 人為暫停（.pause_upload）----------

def test_pause_file_stops_drain_before_probing(tmp_path):
    """重推吃的是與上傳同一份後端額度；額度用完時放行只會堆出 failed。

    暫停檢查放在探測之前——暫停時連探測都不必對外連線。
    """
    pf = tmp_path / '.pause_upload'
    pf.write_text('本月 OpenAI 額度已用完\n第二行\n', encoding='utf-8')
    probed = []
    sent = []
    rc = ds.main(probe_fn=lambda: probed.append(1) or Verdict(True, 'ok'),
                 pause_file=str(pf), state_path=str(tmp_path / 's.json'), now=NOW,
                 fetch_fn=lambda: [_r('1', 'a.pdf', 'pending')], our_names=OURS,
                 retry_fn=_retry({'accepted': 1}))
    assert rc == ds.EXIT_PAUSED == 6
    assert probed == [], '暫停時不該對外探測'
    assert sent == [], '暫停時不該送出任何重推'


def test_no_pause_file_proceeds(tmp_path):
    sent = []
    rc = ds.main(probe_fn=lambda: Verdict(True, 'ok'),
                 pause_file=str(tmp_path / 'nope'), state_path=str(tmp_path / 's.json'), now=NOW,
                 fetch_fn=lambda: [_r('1', 'a.pdf', 'pending')], our_names=OURS,
                 retry_fn=lambda i, stats=None: (sent.append(i),
                                                 stats.update({'accepted': 1}) if stats else None, 0)[-1])
    assert rc == 0 and sent == [['1']]


def test_empty_pause_file_still_pauses(tmp_path):
    """空的旗標檔也算暫停——存在即是意圖。"""
    pf = tmp_path / '.pause_upload'
    pf.write_text('', encoding='utf-8')
    rc = ds.main(probe_fn=lambda: Verdict(True, 'ok'), pause_file=str(pf),
                 state_path=str(tmp_path / 's.json'), now=NOW,
                 fetch_fn=lambda: [], our_names=OURS, retry_fn=_retry({}))
    assert rc == ds.EXIT_PAUSED


def test_pause_exit_code_distinct_from_held_and_failure():
    """三種結束碼語意不可混：0 正常／4 真異常／5 引擎異常／6 人為暫停。"""
    from geobingan_sync.parser_health import EXIT_PARSER_HELD
    assert len({0, 4, EXIT_PARSER_HELD, ds.EXIT_PAUSED}) == 4


def test_health_check_treats_pause_exit_as_normal():
    import re as _re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'health_check.py'), encoding='utf-8').read()
    m = _re.search(r"'drainstuck': \{([^}]*)\}", src)
    assert m and 'EXIT_PAUSED' in m.group(1) and 'EXIT_PARSER_HELD' in m.group(1)
    assert '4' not in m.group(1), 'exit 4 仍不可列為正常'
