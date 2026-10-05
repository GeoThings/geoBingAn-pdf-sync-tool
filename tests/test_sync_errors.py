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
    assert st['last_run'] == {'at': NOW.isoformat(), 'visited': 3, 'errored': 1, 'run': 'R1'}


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
    state = {'last_run': {'at': NOW.isoformat(), 'visited': 452, 'errored': 10}}
    level, msg = health_check.check_sync_errors(path=_write(tmp_path, state), now=NOW)
    assert level == 'ok'
    assert '10 / 452' in msg and '暫時性' in msg


def test_check_reports_systemic_as_error(tmp_path):
    state = {'last_run': {'at': NOW.isoformat(), 'visited': 452, 'errored': 60}}
    level, msg = health_check.check_sync_errors(path=_write(tmp_path, state), now=NOW)
    assert level == 'error' and '系統性' in msg


def test_check_reports_stuck_permits_as_error(tmp_path):
    state = {'last_run': {'at': NOW.isoformat(), 'visited': 452, 'errored': 2},
             'error_streak': {'A': 4, 'B': 3}}
    level, msg = health_check.check_sync_errors(path=_write(tmp_path, state), now=NOW)
    assert level == 'error'
    assert 'A(4 輪)' in msg and '2 案連續' in msg


def test_check_clean_run_is_ok(tmp_path):
    state = {'last_run': {'at': NOW.isoformat(), 'visited': 452, 'errored': 0}}
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
    from geobingan_sync.steps.sync_permits import STATE_FILE
    src = pathlib_read(os.path.join(ROOT, 'health_check.py'))
    m = re.search(r"^SYNC_PROGRESS_FILE = '([^']+)'", src, re.M)
    assert m, 'health_check 找不到 SYNC_PROGRESS_FILE 的字面定義'
    assert m.group(1) == STATE_FILE, (
        f'兩邊路徑漂開了: health_check={m.group(1)!r} sync_permits={STATE_FILE!r}')
