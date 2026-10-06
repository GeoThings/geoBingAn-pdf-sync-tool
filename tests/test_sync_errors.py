"""同步錯誤可見度：落檔帶時間戳、連續失敗結算、保留窗、健康檢查門檻。

這支功能的起點是 2026-10-05 的實測：一輪 10 筆 Connection reset／讀取逾時，
exit code 0、無人告警，手動 grep 日誌才看到；而且 errors 項目沒有時間戳
（正式環境 2,067 筆），連「今天有沒有出錯」都答不出來。
"""
import json
from datetime import datetime, timedelta

import pytest

import health_check
from geobingan_sync import sync_errors as se

ROOT = __import__('os').path.dirname(__import__('os').path.dirname(
    __import__('os').path.abspath(__file__)))


def pathlib_read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()

NOW = datetime(2026, 10, 5, 11, 0, 0)


# ---------- 落檔一定帶時間戳 ----------

def test_recorded_error_always_carries_a_timestamp():
    """沒有時間戳的錯誤紀錄無法回答任何「何時」的問題——那是原始缺陷本身。"""
    st = {}
    e = se.record_error(st, 'A', 'boom', now=NOW)
    assert e['at'] == NOW.isoformat()
    assert st['errors'] == [e]
    assert e['permit'] == 'A' and e['error'] == 'boom'


def test_recorded_error_keeps_run_id_when_given():
    st = {}
    se.record_error(st, 'A', 'boom', now=NOW, run='2026-10-05T10:00:05')
    assert st['errors'][0]['run'] == '2026-10-05T10:00:05'


def test_exception_objects_are_stringified():
    st = {}
    se.record_error(st, 'A', OSError(54, 'Connection reset by peer'), now=NOW)
    assert 'Connection reset by peer' in st['errors'][0]['error']


# ---------- 連續失敗在輪次層結算 ----------

def test_streak_increments_only_once_per_run_even_with_many_errors():
    """同一案一輪記了三筆錯誤，連續輪數只能 +1。

    adapter 路徑會對同一案同時記「部分檔案失敗」又標 processed；若在事件層
    加減，一案多錯就會虛增輪數、提早達標誤報。
    """
    st = {}
    for _ in range(3):
        se.record_error(st, 'A', 'boom', now=NOW)
    res = se.finalize_run(st, visited={'A'}, errored={'A'}, now=NOW)
    assert st['error_streak']['A'] == 1
    assert res['errored'] == 1 and res['visited'] == 1


def test_streak_reaches_threshold_on_the_third_run_not_the_second():
    st = {}
    for i in range(2):
        res = se.finalize_run(st, {'A'}, {'A'}, now=NOW + timedelta(days=i))
        assert res['newly_stuck'] == [], f'第 {i+1} 輪不該達標'
    res = se.finalize_run(st, {'A'}, {'A'}, now=NOW + timedelta(days=2))
    assert res['newly_stuck'] == ['A']
    assert st['error_streak']['A'] == se.ERROR_STREAK_ALERT


def test_newly_stuck_fires_only_on_the_crossing_run():
    """跨過門檻那一輪回報一次；之後持續失敗由 stuck 清單承接，不每輪重報。"""
    st = {}
    for i in range(3):
        se.finalize_run(st, {'A'}, {'A'}, now=NOW + timedelta(days=i))
    res = se.finalize_run(st, {'A'}, {'A'}, now=NOW + timedelta(days=3))
    assert res['newly_stuck'] == []
    assert res['stuck'] == [('A', 4)]


def test_success_resets_streak_and_is_reported():
    """先前失敗、這輪成功 → 歸零並出聲。恢復無聲等於沒偵測到。"""
    st = {}
    se.finalize_run(st, {'A'}, {'A'}, now=NOW)
    res = se.finalize_run(st, {'A'}, set(), now=NOW + timedelta(days=1))
    assert res['recovered'] == ['A']
    assert 'A' not in st['error_streak']


def test_clean_permit_never_appears_in_recovered():
    """沒有先前失敗就談不上恢復，否則第一輪會把全部建案報成好消息。"""
    st = {}
    res = se.finalize_run(st, {'A', 'B'}, set(), now=NOW)
    assert res['recovered'] == []


def test_unvisited_permits_are_neither_bumped_nor_reset():
    """同步中途掛掉時，沒走訪到的案不可被誤判成「這輪沒錯」。

    同 feedback_negative_cache_needs_expiry：只有實際重打過的才更新狀態。
    """
    st = {}
    se.finalize_run(st, {'A'}, {'A'}, now=NOW)            # A 失敗一輪
    se.finalize_run(st, {'B'}, set(), now=NOW + timedelta(days=1))   # 這輪只走到 B
    assert st['error_streak']['A'] == 1, 'A 沒走訪到，不該被歸零'
    res = se.finalize_run(st, {'A'}, {'A'}, now=NOW + timedelta(days=2))
    assert st['error_streak']['A'] == 2


def test_errored_must_be_a_subset_of_visited():
    """分子不可大於分母——錯誤集合若含未走訪的案，比率就失去意義（同 #109）。"""
    st = {}
    res = se.finalize_run(st, visited={'A'}, errored={'A', 'GHOST'}, now=NOW)
    assert res['errored'] == 1 and res['visited'] == 1
    assert 'GHOST' not in st['error_streak']


def test_last_run_records_the_conclusive_counts():
    st = {}
    se.finalize_run(st, {'A', 'B', 'C'}, {'A'}, now=NOW, run='R1')
    assert st['last_run'] == {'at': NOW.isoformat(), 'visited': 3, 'errored': 1,
                              'completed': True, 'run': 'R1'}


# ---------- 舊資料搬遷：先搬再修剪 ----------

def test_legacy_entries_are_summarised_not_deleted():
    """無時間戳的舊項目壓成摘要。直接刪會失去「某案一直失敗」的歷史。"""
    st = {'errors': [{'permit': 'A', 'error': 'Invalid URL ID'},
                     {'permit': 'A', 'error': 'Invalid URL ID'},
                     {'permit': 'B', 'error': 'x', 'at': NOW.isoformat()}]}
    moved = se.migrate_legacy(st)
    assert moved == 2
    leg = st[se.LEGACY_KEY]
    assert leg['count'] == 2
    assert leg['by_permit'] == {'A': 2}, '「某案失敗幾次」是要保住的那個訊號'
    assert leg['by_error'] == {'Invalid URL ID': 2}
    assert len(leg['sample']) == 2
    assert [e['permit'] for e in st['errors']] == ['B']


def test_legacy_summary_is_bounded_by_permit_count_not_entry_count():
    """摘要大小由建照數綁住，不隨錯誤筆數成長——這是壓成摘要的理由。"""
    st = {'errors': [{'permit': f'P{i % 50}', 'error': f'boom {i}'} for i in range(5000)]}
    se.migrate_legacy(st)
    leg = st[se.LEGACY_KEY]
    assert leg['count'] == 5000
    assert len(leg['by_permit']) == 50
    assert len(leg['by_error']) <= se.LEGACY_TOP_ERRORS
    assert len(leg['sample']) == se.LEGACY_SAMPLE


def test_migration_is_idempotent():
    st = {'errors': [{'permit': 'A', 'error': 'x'}]}
    assert se.migrate_legacy(st) == 1
    assert se.migrate_legacy(st) == 0
    assert st[se.LEGACY_KEY]['count'] == 1


def test_second_migration_accumulates_instead_of_overwriting():
    """第二批無時間戳項目（手動編輯、舊版回退）要累加，不可重算或清掉第一批。"""
    st = {'errors': [{'permit': 'A', 'error': 'x'}]}
    se.migrate_legacy(st)
    st['errors'].append({'permit': 'A', 'error': 'x'})
    assert se.migrate_legacy(st) == 1
    assert st[se.LEGACY_KEY]['count'] == 2
    assert st[se.LEGACY_KEY]['by_permit'] == {'A': 2}


def test_migration_folds_an_old_list_shaped_legacy_key():
    """相容：先前版本把 errors_legacy 寫成 list，不可因此爆掉或蓋掉。"""
    st = {se.LEGACY_KEY: [{'permit': 'OLD', 'error': 'e'}],
          'errors': [{'permit': 'A', 'error': 'x'}]}
    assert se.migrate_legacy(st) == 1
    leg = st[se.LEGACY_KEY]
    assert isinstance(leg, dict) and leg['count'] == 1
    assert any(e.get('permit') == 'OLD' for e in leg['sample'])


def test_migration_scales_to_the_real_backlog():
    """正式環境實測 2,067 筆，筆數要全部算到，errors 要清空。"""
    st = {'errors': [{'permit': f'P{i % 480}', 'error': 'e'} for i in range(2067)]}
    assert se.migrate_legacy(st) == 2067
    assert st[se.LEGACY_KEY]['count'] == 2067
    assert sum(st[se.LEGACY_KEY]['by_permit'].values()) == 2067
    assert st['errors'] == []


def test_trim_keeps_recent_drops_old():
    old = (NOW - timedelta(days=se.KEEP_ERROR_DAYS + 1)).isoformat()
    st = {'errors': [{'permit': 'OLD', 'error': 'e', 'at': old},
                     {'permit': 'NEW', 'error': 'e', 'at': NOW.isoformat()}]}
    assert se.trim_errors(st, now=NOW) == 1
    assert [e['permit'] for e in st['errors']] == ['NEW']


def test_trim_enforces_max_entries_keeping_the_newest():
    st = {'errors': [{'permit': f'P{i}', 'error': 'e', 'at': NOW.isoformat()}
                     for i in range(se.MAX_ERRORS + 10)]}
    se.trim_errors(st, now=NOW)
    assert len(st['errors']) == se.MAX_ERRORS
    assert st['errors'][-1]['permit'] == f'P{se.MAX_ERRORS + 9}'


def test_trim_never_touches_the_streak_counters():
    """修剪是為了檔案不要無限長；把結論一起清掉就等於偷偷關掉告警。"""
    old = (NOW - timedelta(days=90)).isoformat()
    st = {'errors': [{'permit': 'A', 'error': 'e', 'at': old}],
          'error_streak': {'A': 5}}
    se.trim_errors(st, now=NOW)
    assert st['errors'] == []
    assert st['error_streak'] == {'A': 5}
    assert se.stuck_permits(st) == [('A', 5)]


def test_trim_leaves_undated_entries_for_the_migration():
    """無時間戳的不在修剪這一步丟——否則先修剪就會在搬遷前先失去歷史。"""
    st = {'errors': [{'permit': 'A', 'error': 'e'}]}
    assert se.trim_errors(st, now=NOW) == 0
    assert len(st['errors']) == 1


def test_trim_does_not_apply_the_entry_cap_to_undated_entries():
    """關鍵：筆數上限也不可套用在無時間戳項目上。

    只讓日期規則跳過它們是不夠的——正式環境 2,067 筆全部通過日期規則後會一起
    撞上 max_entries，被砍到剩 500。那樣安全就變成「依賴 migrate 先跑」，是
    呼叫點自律而不是不變式（同 feedback_enforce_invariant_at_boundary）。
    所以這條刻意**不先呼叫 migrate_legacy**。
    """
    st = {'errors': [{'permit': f'P{i}', 'error': 'e'} for i in range(2067)]}
    dropped = se.trim_errors(st, now=NOW)
    assert dropped == 0, f'無時間戳項目不該被修剪掉任何一筆，卻掉了 {dropped}'
    assert len(st['errors']) == 2067
    # 修剪之後才搬遷，一筆不少
    assert se.migrate_legacy(st) == 2067


def test_trim_caps_dated_entries_even_when_undated_ones_are_present():
    """反面：有時間戳的仍要受上限約束，別為了修上一條把上限整個放掉。"""
    st = {'errors': [{'permit': 'OLDFMT', 'error': 'e'}]
                    + [{'permit': f'P{i}', 'error': 'e', 'at': NOW.isoformat()}
                       for i in range(se.MAX_ERRORS + 10)]}
    se.trim_errors(st, now=NOW)
    dated = [e for e in st['errors'] if e.get('at')]
    undated = [e for e in st['errors'] if not e.get('at')]
    assert len(dated) == se.MAX_ERRORS
    assert len(undated) == 1


# ---------- summarise：未結算不可讀成 0 ----------

def test_summarise_without_last_run_is_unknown_not_zero():
    """「還沒結算」與「結算出零錯誤」必須分得開。

    2026-10-05 我自己把前者讀成後者：step_result 時間戳是三天前、synced=0，
    真相是同步還在跑。
    """
    res = se.summarise_last_run({})
    assert res['known'] is False
    assert res['errored'] is None and res['visited'] is None and res['rate'] is None
    assert res['systemic'] is False


@pytest.mark.parametrize('bad', [{'visited': 'x', 'errored': 1},
                                 {'visited': 3},
                                 {'errored': 1},
                                 {}])
def test_summarise_rejects_unusable_last_run(bad):
    assert se.summarise_last_run({'last_run': bad})['known'] is False


def test_summarise_zero_errors_is_known():
    st = {}
    se.finalize_run(st, {'A', 'B'}, set(), now=NOW)
    res = se.summarise_last_run(st)
    assert res['known'] is True and res['errored'] == 0 and res['systemic'] is False


# ---------- 門檻校準：釘住 2026-10-05 的實際數字 ----------

def test_the_2026_10_05_transient_blip_must_not_alert():
    """實測 10 / 452 案（2.2%）是網路抖動打中執行緒池，不該亮燈。

    這條是門檻的校準基準：執行緒池 5 條，抖動規模 5–10 筆是常態。
    """
    st = {}
    visited = {f'P{i}' for i in range(452)}
    errored = {f'P{i}' for i in range(10)}
    se.finalize_run(st, visited, errored, now=NOW)
    res = se.summarise_last_run(st)
    assert res['errored'] == 10 and res['visited'] == 452
    assert res['systemic'] is False, f"2.2% 不該判成系統性: {res['rate']}"
    assert res['stuck'] == []


def test_systemic_failure_alerts():
    """錯誤佔比達門檻＝憑證失效或網路斷，不是個案抖動。"""
    st = {}
    visited = {f'P{i}' for i in range(452)}
    errored = {f'P{i}' for i in range(23)}          # 23/452 = 5.1%
    se.finalize_run(st, visited, errored, now=NOW)
    assert se.summarise_last_run(st)['systemic'] is True


def test_rate_threshold_boundary_is_inclusive():
    st = {}
    se.finalize_run(st, {f'P{i}' for i in range(100)},
                    {f'P{i}' for i in range(5)}, now=NOW)       # 正好 5%
    assert se.summarise_last_run(st)['systemic'] is True


def test_zero_visited_never_divides_by_zero():
    st = {}
    se.finalize_run(st, set(), set(), now=NOW)
    res = se.summarise_last_run(st)
    assert res['rate'] == 0.0 and res['systemic'] is False


# ---------- health_check 第 12 項 ----------

def _write(tmp_path, state):
    p = tmp_path / 'sync_permits_progress.json'
    p.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    return str(p)


def _sync_status(tmp_path, when):
    p = tmp_path / 'sync_status.json'
    p.write_text(json.dumps({'last_run': when.isoformat()}), encoding='utf-8')
    return str(p)


def test_check_missing_file_is_ok():
    level, msg = health_check.check_sync_errors(path='/nonexistent/x.json', now=NOW)
    assert level == 'ok' and '尚未跑過' in msg


def test_check_corrupt_file_is_not_a_green_light(tmp_path):
    p = tmp_path / 'sync_permits_progress.json'
    p.write_text('{ not json', encoding='utf-8')
    level, msg = health_check.check_sync_errors(path=str(p), now=NOW)
    assert level == 'warning' and '損毀' in msg


def test_check_unfinalized_warns_when_sync_itself_is_fine(tmp_path):
    """沒結算不可當綠燈——結算失敗刻意不中斷同步，代價就是會無聲。"""
    path = _write(tmp_path, {'processed': {'A': True}})
    level, msg = health_check.check_sync_errors(
        path=path, now=NOW, sync_status_path=_sync_status(tmp_path, NOW - timedelta(hours=1)))
    assert level == 'warning' and '無從判斷' in msg


def test_check_unfinalized_defers_when_sync_already_alerts(tmp_path):
    """同步本身已經在告警時讓給它報，不要兩支亮同一件事。

    門檻不複製、直接問 check_last_sync——自己寫一份 48 小時門檻會在
    「3～10 天前同步過」那段區間兩邊同時綠燈，出現告警真空（同 #103）。
    """
    path = _write(tmp_path, {'processed': {}})
    level, msg = health_check.check_sync_errors(
        path=path, now=NOW, sync_status_path=_sync_status(tmp_path, NOW - timedelta(days=30)))
    assert level == 'ok' and '已另行告警' in msg


def test_check_stale_finalization_warns(tmp_path):
    state = {'last_run': {'at': (NOW - timedelta(hours=72)).isoformat(),
                          'visited': 400, 'errored': 0}}
    level, msg = health_check.check_sync_errors(
        path=_write(tmp_path, state), now=NOW,
        sync_status_path=_sync_status(tmp_path, NOW - timedelta(hours=1)))
    assert level == 'warning' and '未更新' in msg


def test_check_reports_transient_as_ok(tmp_path):
    state = {'last_run': {'at': NOW.isoformat(), 'visited': 452, 'errored': 10, 'completed': True}}
    level, msg = health_check.check_sync_errors(path=_write(tmp_path, state), now=NOW)
    assert level == 'ok'
    assert '10 / 452' in msg and '暫時性' in msg


def test_check_reports_systemic_as_error(tmp_path):
    state = {'last_run': {'at': NOW.isoformat(), 'visited': 452, 'errored': 60, 'completed': True}}
    level, msg = health_check.check_sync_errors(path=_write(tmp_path, state), now=NOW)
    assert level == 'error' and '系統性' in msg


def test_check_reports_stuck_permits_as_error(tmp_path):
    state = {'last_run': {'at': NOW.isoformat(), 'visited': 452, 'errored': 2, 'completed': True},
             'error_streak': {'A': 4, 'B': 3}}
    level, msg = health_check.check_sync_errors(path=_write(tmp_path, state), now=NOW)
    assert level == 'error'
    assert 'A(4 輪)' in msg and '2 案連續' in msg


def test_check_clean_run_is_ok(tmp_path):
    state = {'last_run': {'at': NOW.isoformat(), 'visited': 452, 'errored': 0, 'completed': True}}
    level, msg = health_check.check_sync_errors(path=_write(tmp_path, state), now=NOW)
    assert level == 'ok' and '全部無錯誤' in msg


def test_check_is_registered_in_the_checks_list():
    """寫了檢查卻沒掛進清單＝永遠不會跑。"""
    names = [n for n, _ in health_check.DEFAULT_CHECKS]
    assert '同步錯誤' in names
    assert health_check.check_sync_errors in [f for _, f in health_check.DEFAULT_CHECKS]


# ---------- 防漂移：兩邊的進度檔路徑必須相同 ----------

def test_health_check_progress_path_matches_sync_permits():
    """health_check 刻意不 import sync_permits（會拉進 google-api 整套），
    代價是路徑各寫一份。用測試釘住相等，否則改了一邊就靜默讀錯檔。

    比的是**原始碼裡的字面值**：conftest 的 autouse fixture 會把模組屬性指到
    暫存檔（那是為了不讀到正式狀態），拿執行期的值來比永遠不相等。
    """
    import os
    import re
    hc = pathlib_read(os.path.join(ROOT, 'health_check.py'))
    sp = pathlib_read(os.path.join(ROOT, 'geobingan_sync', 'steps', 'sync_permits.py'))
    a = re.search(r"^SYNC_PROGRESS_FILE = '([^']+)'", hc, re.M)
    b = re.search(r"^STATE_FILE = '([^']+)'", sp, re.M)
    assert a, 'health_check 找不到 SYNC_PROGRESS_FILE 的字面定義'
    assert b, 'sync_permits 找不到 STATE_FILE 的字面定義'
    assert a.group(1) == b.group(1), (
        f'兩邊路徑漂開了: health_check={a.group(1)!r} sync_permits={b.group(1)!r}')


# ---------- review P1：呼叫點漏接 ----------
#
# 第一版只把三個呼叫點改走 _record_error()，漏了另外三個（fetch_all 整包失敗、
# list_files 列檔失敗、Invalid URL ID）。後果比「少帶時間戳」嚴重：那些案**已經
# 走訪**但不在 _errored_permits，於是 finalize_run 把它們當成功，**反而清掉既有
# 的 error_streak**——連續失敗永遠累積不起來。
# 而 Invalid URL ID 在正式環境歷史上有 538 次，是最多的錯誤類別之一。
#
# 抽出共用函式只單一化了規則，擋不住呼叫點漏接（同
# feedback_shared_instance_defaults_outlive_session）。所以除了逐一修，還要有
# 一條掃原始碼的守門，讓第七個呼叫點不會重犯。

import re as _re


def _sync_permits_source():
    import os
    return pathlib_read(os.path.join(ROOT, 'geobingan_sync', 'steps', 'sync_permits.py'))


def test_no_call_site_appends_to_errors_directly():
    """錯誤落檔只能走 _record_error()。直接 append 會繞過時間戳與 _errored_permits。

    掃原始碼時**先去掉註解行**——否則這支測試會被自己的說明文字騙過去
    （同類假綠在 #101/#104/#105 各踩過一次）。
    """
    lines = [l for l in _sync_permits_source().split('\n')
             if not l.lstrip().startswith('#')]
    bad = [f'line {i}: {l.strip()[:70]}' for i, l in enumerate(lines, 1)
           if _re.search(r"state\['errors'\]\s*\.\s*append", l)]
    assert not bad, ('這些呼叫點繞過 _record_error()，錯誤不會帶時間戳、'
                     '也不會進 _errored_permits：\n  ' + '\n  '.join(bad))


def test_record_error_is_the_only_writer_and_is_used_everywhere():
    """防假綠的反面：確認掃描器真的看得到 _record_error 的呼叫，不是掃了空氣。"""
    src = _sync_permits_source()
    assert src.count('self._record_error(') >= 6, (
        f'只找到 {src.count("self._record_error(")} 個呼叫點，'
        '預期至少 6（bundle 寫入／bundle 整包／adapter 列檔／adapter 取檔／'
        'sync_permit 例外／executor 未預期／Invalid URL ID）')


class _BoomBundle:
    """有 fetch_all → 走 bundle 路徑，而且整包取得就炸。"""
    name = 'boom-bundle'

    def fetch_all(self, url):
        from geobingan_sync.source_adapters.base import AdapterError
        raise AdapterError('ReadTimeout:整包下載逾時')


class _BoomList:
    """沒有 fetch_all → 走逐檔路徑，列檔就炸。"""
    name = 'boom-list'

    def list_files(self, url):
        from geobingan_sync.source_adapters.base import AdapterError
        raise AdapterError('HTTPError:403')


def _permit_sync(tmp_path):
    from geobingan_sync.steps.sync_permits import PermitSync
    csv = tmp_path / 'c.csv'
    csv.write_text('permit_no,source_url,name\n', encoding='utf-8')
    ps = PermitSync(city={'csv_path': str(csv), 'source_type': 'csv'})
    # bundle 路徑在 try 之前會 preload_target_files()，那一步要 Drive 憑證。
    # 它不是這裡要驗的行為（我們驗的是錯誤怎麼被記），所以 stub 掉。
    ps.preload_target_files = lambda *a, **k: None
    return ps


@pytest.mark.parametrize('adapter_cls,expect', [
    (_BoomBundle, 'ReadTimeout'),
    (_BoomList, 'HTTPError'),
])
def test_adapter_failures_are_recorded_with_timestamp_and_marked_errored(
        tmp_path, adapter_cls, expect):
    ps = _permit_sync(tmp_path)
    ps.state = {'processed': {}, 'errors': []}
    ps._sync_via_adapter('112建字第0125號', 'https://x.test/a', 'TARGET', adapter_cls())

    assert len(ps.state['errors']) == 1, '錯誤必須落檔'
    e = ps.state['errors'][0]
    assert e['permit'] == '112建字第0125號'
    assert expect in e['error']
    assert e.get('at'), '必須帶時間戳，否則答不出「何時」'
    assert '112建字第0125號' in ps._errored_permits, (
        '沒進 _errored_permits → finalize_run 會把它當成功並清掉 streak')


def test_invalid_url_id_is_recorded(tmp_path):
    """正式環境歷史上 538 次，是最多的錯誤類別之一，原本完全不會進資料。"""
    ps = _permit_sync(tmp_path)
    ps.state = {'processed': {}, 'errors': []}
    ps.sync_permit('109建字第0019號', 'https://unknown-host.test/x', 'TARGET')

    assert [e['permit'] for e in ps.state['errors']] == ['109建字第0019號']
    assert ps.state['errors'][0]['error'] == 'Invalid URL ID'
    assert ps.state['errors'][0].get('at')
    assert '109建字第0019號' in ps._errored_permits
    assert '109建字第0019號' in ps._visited_permits, '走訪也要標，否則不參與結算'


@pytest.mark.parametrize('adapter_cls', [_BoomBundle, _BoomList])
def test_adapter_failure_increments_streak_instead_of_resetting_it(tmp_path, adapter_cls):
    """這條是 P1 真正的傷害：原本會把已失敗的案判成成功、把 streak 歸零。"""
    ps = _permit_sync(tmp_path)
    ps.state = {'processed': {}, 'errors': [], 'error_streak': {'112建字第0125號': 2}}
    ps._sync_via_adapter('112建字第0125號', 'https://x.test/a', 'TARGET', adapter_cls())
    ps._visited_permits.add('112建字第0125號')

    res = se.finalize_run(ps.state, ps._visited_permits, ps._errored_permits, now=NOW)
    assert ps.state['error_streak']['112建字第0125號'] == 3, '應累積到 3 而不是歸零'
    assert res['newly_stuck'] == ['112建字第0125號']
    assert res['recovered'] == [], '失敗的案不可被報成恢復'


# ---------- 2026-10-06 實跑踩到：run() 拋錯時結算整段跳過 ----------
#
# 當天掃描共享雲端逾時 → run() 拋錯 → 城市層（main 的迴圈）只 print 不重拋 →
# _finalize_errors() 是 run() 最後一行，整段跳過。搬遷沒跑、last_run 沒寫。
#
# 但光搬進 finally 會製造假綠：那輪走訪 0 案，last_run 會是
# {visited: 0, errored: 0}，讀起來就是「0 案全部無錯誤」。所以「有沒有跑完」
# 要跟數字一起存（同 #108：放進 finally 不可把失敗記成成功）。

def test_finalize_records_completed_flag():
    st = {}
    se.finalize_run(st, {'A'}, set(), now=NOW, completed=True)
    assert st['last_run']['completed'] is True
    se.finalize_run(st, {'A'}, set(), now=NOW, completed=False)
    assert st['last_run']['completed'] is False


def test_finalize_records_run_level_error():
    """掃描逾時這類錯誤不屬於任何單一建案，但必須在資料裡留下痕跡。"""
    st = {}
    se.finalize_run(st, set(), set(), now=NOW, completed=False,
                    run_error='TimeoutError: The read operation timed out')
    assert 'read operation timed out' in st['last_run']['error']


def test_incomplete_run_is_never_clean():
    st = {}
    se.finalize_run(st, {'A', 'B'}, set(), now=NOW, completed=False)
    res = se.summarise_last_run(st)
    assert res['known'] is True
    assert res['completed'] is False
    assert res['clean'] is False, '未跑完不可被當成乾淨的一輪'


def test_zero_visited_is_never_clean():
    """2026-10-06 那輪：逐案迴圈之前就死了，什麼都沒量到。"""
    st = {}
    se.finalize_run(st, set(), set(), now=NOW, completed=False)
    res = se.summarise_last_run(st)
    assert res['visited'] == 0 and res['errored'] == 0
    assert res['clean'] is False, '0 案全部無錯誤 ≠ 這輪乾淨'


def test_zero_visited_is_not_clean_even_when_marked_completed():
    """就算旗標說跑完了，走訪 0 案仍然什麼都沒量到。"""
    st = {}
    se.finalize_run(st, set(), set(), now=NOW, completed=True)
    assert se.summarise_last_run(st)['clean'] is False


def test_legacy_last_run_without_completed_is_not_assumed_successful():
    """舊格式紀錄沒有 completed 欄位 → 當成「不知道」，不可偏向綠燈那側。"""
    st = {'last_run': {'at': NOW.isoformat(), 'visited': 400, 'errored': 0}}
    res = se.summarise_last_run(st)
    assert res['completed'] is None
    assert res['clean'] is False


def test_completed_run_with_no_errors_is_clean():
    """反面：真的跑完、真的走訪過、真的零錯誤，才算乾淨。"""
    st = {}
    se.finalize_run(st, {'A', 'B'}, set(), now=NOW, completed=True)
    assert se.summarise_last_run(st)['clean'] is True


def test_check_reports_incomplete_run_as_error(tmp_path):
    state = {'last_run': {'at': NOW.isoformat(), 'visited': 0, 'errored': 0,
                          'completed': False,
                          'error': 'TimeoutError: The read operation timed out'}}
    level, msg = health_check.check_sync_errors(path=_write(tmp_path, state), now=NOW)
    assert level == 'error'
    assert '未跑完' in msg and 'read operation timed out' in msg


def test_check_reports_zero_visited_as_warning(tmp_path):
    state = {'last_run': {'at': NOW.isoformat(), 'visited': 0, 'errored': 0,
                          'completed': True}}
    level, msg = health_check.check_sync_errors(path=_write(tmp_path, state), now=NOW)
    assert level == 'warning'
    assert '走訪 0 個建案' in msg
    assert '沒有錯誤' in msg, '要明講這不等於沒有錯誤'


def test_check_reports_legacy_record_as_warning(tmp_path):
    state = {'last_run': {'at': NOW.isoformat(), 'visited': 400, 'errored': 0}}
    level, msg = health_check.check_sync_errors(path=_write(tmp_path, state), now=NOW)
    assert level == 'warning' and '是否跑完' in msg


def test_finalize_runs_even_when_run_raises(tmp_path, monkeypatch):
    """結算必須在 run() 拋錯時也跑到——這是 10/06 真正的缺口。"""
    ps = _permit_sync(tmp_path)
    ps.state = {'processed': {}, 'errors': []}

    def _boom():
        ps._visited_permits.add('112建字第0125號')      # 死掉前已走訪一案
        raise TimeoutError('The read operation timed out')

    monkeypatch.setattr(ps, '_run_steps', _boom)
    with pytest.raises(TimeoutError):
        ps.run()

    assert 'last_run' in ps.state, 'run() 拋錯時結算仍須寫入'
    assert ps.state['last_run']['completed'] is False
    assert 'read operation timed out' in ps.state['last_run']['error']
    assert ps.state['last_run']['visited'] == 1
    assert se.summarise_last_run(ps.state)['clean'] is False


def test_successful_run_marks_completed(tmp_path, monkeypatch):
    ps = _permit_sync(tmp_path)
    ps.state = {'processed': {}, 'errors': []}
    monkeypatch.setattr(ps, '_run_steps', lambda: ps._visited_permits.add('A'))
    ps.run()
    assert ps.state['last_run']['completed'] is True
    assert se.summarise_last_run(ps.state)['clean'] is True


def test_step_results_default_base_is_isolated_in_tests():
    """🔴 conftest 必須把 step_results 的預設路徑指開。

    10/06 查出 test_parse_pdf_list_urls 的 ps.run() 會經由
    _write_step_result() → accumulate()（沒帶 base）寫到 './state'，把 10/05
    真實的 synced=194 覆蓋成 0。防線要擋在模組預設路徑，不是逐一檢查呼叫點。
    """
    from geobingan_sync import step_results
    assert step_results._BASE != './state', (
        'step_results._BASE 沒被隔離，測試會寫到正式 state/')
