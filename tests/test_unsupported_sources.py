"""接不到的來源要成為可追蹤的資料，而且名單必須會縮。

被 `_resolve_or_skip_indirect` 剔除的建案原本在系統裡完全消失——沒有名單、沒有
原因、沒有時間。2026-09-29 逐一探測的結論是缺口大部分不是工程問題（承造人給的
連結需要帳號才看得到），要走對外溝通，而對外溝通需要一份維護中的名單。

名單只增不減就會變成舊帳，所以每輪以「本輪實際被跳過的集合」重寫。
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import health_check
from geobingan_sync import unsupported_sources as us
from geobingan_sync.steps.sync_permits import PermitSync

NOW = datetime(2026, 9, 29, 10, 0)
TODAY = '2026-09-29'


# ---------- 家族分類：依主機名，不是對可用性的判斷 ----------

@pytest.mark.parametrize('host,family', [
    ('cectw-my.sharepoint.com', 'SharePoint'),
    ('sharepoint.com', 'SharePoint'),
    ('1drv.ms', 'OneDrive'),
    ('onedrive.live.com', 'OneDrive'),
    ('gofile.me', 'Synology 分享'),
    ('gofile-37634a444d.tw4.quickconnect.to', 'Synology 分享'),
    ('www.dropbox.com', 'Dropbox'),
    ('mega.nz', 'MEGA'),
    ('e.pcloud.link', 'pCloud'),
    ('docs.google.com', 'Google（非資料夾）'),
    ('125.227.22.67', '自架主機（裸 IP）'),
    ('', '（無主機）'),
])
def test_classify_host(host, family):
    assert us.classify_host(host) == family


def test_lookalike_host_is_not_absorbed_into_a_family():
    """後綴偽裝不可被歸成正牌家族，否則彙總數字會說謊。"""
    assert us.classify_host('sharepoint.com.evil.test').startswith('其他')
    assert us.classify_host('notdropbox.com').startswith('其他')


# ---------- 名單建置 ----------

def _entries():
    return [('A', 'https://cectw-my.sharepoint.com/:f:/g/x/E1', 'no_drive_link'),
            ('B', 'http://gofile.me/3nCZT/Dg4', 'no_drive_link')]


def test_build_records_facts():
    d = us.build(_entries(), now=NOW)
    assert d['count'] == 2
    a = d['sources']['A']
    assert a['host'] == 'cectw-my.sharepoint.com' and a['family'] == 'SharePoint'
    assert a['first_seen'] == TODAY and a['last_seen'] == TODAY
    assert a['note'] == 'no_drive_link'


def test_first_seen_is_preserved_across_runs():
    prev = us.build(_entries(), now=NOW - timedelta(days=30))
    d = us.build(_entries(), previous=prev, now=NOW)
    assert d['sources']['A']['first_seen'] == (NOW - timedelta(days=30)).strftime('%Y-%m-%d')
    assert d['sources']['A']['last_seen'] == TODAY, '每輪都要更新 last_seen'


def test_changed_url_resets_first_seen():
    """連結換了就是新情況。沿用舊日期會謊報「已經壞很久」。"""
    prev = us.build(_entries(), now=NOW - timedelta(days=30))
    changed = [('A', 'https://cectw-my.sharepoint.com/:f:/g/x/NEW', 'no_drive_link')]
    d = us.build(changed, previous=prev, now=NOW)
    assert d['sources']['A']['first_seen'] == TODAY


def test_list_shrinks_when_a_source_becomes_supported():
    """補上 adapter 或對方改了連結之後，名單必須縮——否則變成只增不減的舊帳。"""
    prev = us.build(_entries(), now=NOW - timedelta(days=1))
    d = us.build([_entries()[0]], previous=prev, now=NOW)
    assert set(d['sources']) == {'A'}
    assert d['count'] == 1


def test_summarise_orders_by_count_and_carries_evidence_date():
    entries = [(f'S{i}', 'https://x-my.sharepoint.com/a', '') for i in range(5)]
    entries += [('D1', 'https://www.dropbox.com/scl/fo/a', '')]
    rows = us.summarise(us.build(entries, now=NOW))
    assert rows[0][0] == 'SharePoint' and rows[0][1] == 5
    assert rows[0][2] == '需登入' and rows[0][3] == '2026-09-29', '結論必須連探測日期一起帶出'
    assert rows[1][0] == 'Dropbox'


def test_unknown_family_is_reported_as_unverified():
    rows = us.summarise(us.build([('X', 'https://nowhere.test/a', '')], now=NOW))
    assert rows[0][2] == '未確認', '沒探測過的不可借用同家族結論'


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / 'u.json'
    us.save(us.build(_entries(), now=NOW), str(p))
    assert us.load(str(p))['sources']['B']['family'] == 'Synology 分享'
    assert not list(tmp_path.glob('*.tmp')), '原子寫入不可留下暫存檔'


def test_load_missing_file_is_empty(tmp_path):
    assert us.load(str(tmp_path / 'nope.json')) == {}


# ---------- 管線：跳過的建案必須真的被記下來 ----------

@pytest.fixture
def ps():
    return PermitSync(city={'name': 'T', 'pdf_list_url': 'https://x.test/l.pdf'})


def test_pipeline_records_skipped_permits(ps, tmp_path):
    out = tmp_path / 'u.json'
    mapping = {
        'A': 'https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345',
        'B': 'https://public.redsun.tw/site/23',                       # 有 adapter
        'C': 'https://cectw-my.sharepoint.com/:f:/g/personal/x/Ett',   # 接不到
        'D': 'http://gofile.me/3nCZT/Dg4yzeJWI',                       # 接不到
    }

    from geobingan_sync.link_resolver import Resolution
    unresolved = Resolution(folder_id=None, method='none', note='no_drive_link:http200')
    kept = ps._resolve_or_skip_indirect(dict(mapping), resolver=lambda u: unresolved,
                                        cache_path=str(tmp_path / 'c.json'),
                                        unsupported_path=str(out))
    assert set(kept) == {'A', 'B'}
    rec = json.loads(out.read_text(encoding='utf-8'))
    assert set(rec['sources']) == {'C', 'D'}, '被剔除的必須留下紀錄'
    assert rec['sources']['C']['family'] == 'SharePoint'
    assert rec['sources']['C']['note'] == 'no_drive_link:http200'


def test_pipeline_does_not_record_supported_permits(ps, tmp_path):
    out = tmp_path / 'u.json'
    ps._resolve_or_skip_indirect({'B': 'https://public.redsun.tw/site/23'},
                                 cache_path=str(tmp_path / 'c.json'),
                                 unsupported_path=str(out))
    assert json.loads(out.read_text(encoding='utf-8'))['sources'] == {}


def test_record_failure_does_not_break_sync(ps, tmp_path, capsys):
    """名單寫不進去不可害整輪同步失敗——它是可見度，不是同步本身。"""
    kept = ps._resolve_or_skip_indirect(
        {'A': 'https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345',
         'C': 'https://cectw-my.sharepoint.com/:f:/g/x/E'},
        resolver=lambda u: (_ for _ in ()).throw(RuntimeError('x')),
        cache_path=str(tmp_path / 'c.json'),
        unsupported_path=str(tmp_path / 'no_such_dir' / 'sub' / '\0bad'))
    assert 'A' in kept
    assert '名單寫入失敗' in capsys.readouterr().out


# ---------- 健康檢查 ----------

def _write(tmp_path, entries, now=NOW, previous=None):
    p = tmp_path / 'u.json'
    us.save(us.build(entries, previous=previous, now=now), str(p))
    return str(p)


def test_health_ok_when_no_file(tmp_path):
    level, msg = health_check.check_unsupported_sources(path=str(tmp_path / 'nope.json'))
    assert level == 'ok' and '尚未跑過' in msg


def test_health_ok_when_nothing_unsupported(tmp_path):
    level, msg = health_check.check_unsupported_sources(path=_write(tmp_path, []), now=NOW)
    assert level == 'ok' and '全部接得到' in msg


def test_health_warns_on_newly_unreachable(tmp_path):
    """近期新增代表某個原本接得到的來源剛失聯，是新的資料流失訊號。

    要有上一輪當基準才判定得了「新增」——首輪的情況由
    test_first_run_is_not_reported_as_new_failures 管。
    """
    base = us.build([], now=NOW - timedelta(days=60))
    p = _write(tmp_path, _entries(), now=NOW, previous=base)
    level, msg = health_check.check_unsupported_sources(path=p, now=NOW)
    assert level == 'warning' and '新增' in msg and 'A' in msg


def test_health_ok_when_all_are_old(tmp_path):
    old = us.build(_entries(), now=NOW - timedelta(days=60))
    p = _write(tmp_path, _entries(), now=NOW, previous=old)
    level, msg = health_check.check_unsupported_sources(path=p, now=NOW)
    assert level == 'ok' and '無新增' in msg


def test_health_message_carries_evidence_date(tmp_path):
    old = us.build(_entries(), now=NOW - timedelta(days=60))
    p = _write(tmp_path, _entries(), now=NOW, previous=old)
    _level, msg = health_check.check_unsupported_sources(path=p, now=NOW)
    assert 'SharePoint' in msg and '需登入' in msg and '2026-09-29 探測' in msg


def test_corrupt_file_is_not_a_green_light(tmp_path):
    p = tmp_path / 'u.json'
    p.write_text('{ not json', encoding='utf-8')
    level, msg = health_check.check_unsupported_sources(path=str(p), now=NOW)
    assert level == 'warning' and '損毀' in msg


def test_registered_in_default_checks():
    assert any(fn is health_check.check_unsupported_sources
               for _n, fn in health_check.DEFAULT_CHECKS), '沒註冊就永遠不會被巡到'


def test_first_run_is_not_reported_as_new_failures(tmp_path):
    """第一輪沒有基準，全部 first_seen 都是當天——不可當成 41 件新事故。"""
    p = _write(tmp_path, _entries(), now=NOW)          # previous=None → 首輪
    assert json.loads(open(p, encoding='utf-8').read())['first_run'] is True
    level, msg = health_check.check_unsupported_sources(path=p, now=NOW)
    assert level == 'ok' and '首輪建立基準' in msg


def test_second_run_can_report_new_failures(tmp_path):
    base = us.build([_entries()[0]], now=NOW - timedelta(days=60))
    p = _write(tmp_path, _entries(), now=NOW, previous=base)   # B 是新出現的
    assert json.loads(open(p, encoding='utf-8').read())['first_run'] is False
    level, msg = health_check.check_unsupported_sources(path=p, now=NOW)
    assert level == 'warning' and 'B' in msg


def test_baseline_date_is_preserved(tmp_path):
    base = us.build(_entries(), now=NOW - timedelta(days=60))
    d = us.build(_entries(), previous=base, now=NOW)
    assert d['baseline_since'] == (NOW - timedelta(days=60)).strftime('%Y-%m-%d')


def test_unchanged_list_does_not_warn_on_the_second_run(tmp_path):
    """實測抓到的缺陷：基準剛建立時所有 first_seen 都是當天，
    只看「近 14 天」會把整份名單都算成新事故。"""
    first = us.build(_entries(), now=NOW)                 # 首輪
    p = _write(tmp_path, _entries(), now=NOW, previous=first)   # 次輪，狀況不變
    level, msg = health_check.check_unsupported_sources(path=p, now=NOW)
    assert level == 'ok' and '無新增' in msg, msg


def test_source_appearing_after_baseline_is_flagged(tmp_path):
    first = us.build([_entries()[0]], now=NOW - timedelta(days=3))
    second = us.build(_entries(), previous=first, now=NOW - timedelta(days=2))
    p = _write(tmp_path, _entries(), now=NOW, previous=second)
    level, msg = health_check.check_unsupported_sources(path=p, now=NOW)
    assert level == 'warning' and 'B' in msg and 'A' not in msg.split('：')[1]


# ---------- 可見度機制本身不可無聲失效（review P2）----------
#
# 名單寫入失敗刻意不中斷同步（它是可見度、不是同步本身），代價是失敗會無聲：
# 舊檔一直回報「無新增」綠燈，第一次就寫不出來則永遠回報「尚未跑過」綠燈。
# 那正是這支功能要消滅的那種無聲失效。

def _sync_status(tmp_path, last_run):
    p = tmp_path / 'sync_status.json'
    p.write_text(json.dumps({'last_run': last_run.isoformat(), 'last_status': 'success'}),
                 encoding='utf-8')
    return str(p)


def test_stale_list_is_not_a_green_light(tmp_path):
    """內容正常但 generated_at 已過期 → 名單停止更新，不可回綠燈。"""
    old = us.build(_entries(), now=NOW - timedelta(days=30))
    p = _write(tmp_path, _entries(), now=NOW - timedelta(days=5), previous=old)
    level, msg = health_check.check_unsupported_sources(
        path=p, now=NOW, sync_status_path=_sync_status(tmp_path, NOW))
    assert level == 'warning' and '未更新' in msg, msg


def test_missing_list_after_a_sync_is_a_warning(tmp_path):
    """同步跑過了卻找不到名單 → 寫入從一開始就失敗。"""
    level, msg = health_check.check_unsupported_sources(
        path=str(tmp_path / 'nope.json'), now=NOW,
        sync_status_path=_sync_status(tmp_path, NOW - timedelta(hours=2)))
    assert level == 'warning' and '找不到' in msg, msg


def test_missing_list_without_any_sync_is_ok(tmp_path):
    """全新環境還沒跑過同步，缺檔是正常的。"""
    level, msg = health_check.check_unsupported_sources(
        path=str(tmp_path / 'nope.json'), now=NOW,
        sync_status_path=str(tmp_path / 'no_sync.json'))
    assert level == 'ok' and '尚未跑過' in msg


def test_missing_list_when_sync_itself_stopped_does_not_double_alert(tmp_path):
    """同步本身停了由 check_last_sync 報，這裡不重複告警。"""
    level, msg = health_check.check_unsupported_sources(
        path=str(tmp_path / 'nope.json'), now=NOW,
        sync_status_path=_sync_status(tmp_path, NOW - timedelta(days=9)))
    assert level == 'ok' and '同步已停' in msg


@pytest.mark.parametrize('gen', [None, '', 'not-a-date', 12345])
def test_unreadable_generated_at_is_a_warning(tmp_path, gen):
    p = tmp_path / 'u.json'
    d = us.build(_entries(), now=NOW)
    d['generated_at'] = gen
    p.write_text(json.dumps(d, ensure_ascii=False), encoding='utf-8')
    level, msg = health_check.check_unsupported_sources(
        path=str(p), now=NOW, sync_status_path=_sync_status(tmp_path, NOW))
    assert level == 'warning' and 'generated_at' in msg


def test_fresh_list_still_reports_normally(tmp_path):
    old = us.build(_entries(), now=NOW - timedelta(days=60))
    p = _write(tmp_path, _entries(), now=NOW, previous=old)
    level, msg = health_check.check_unsupported_sources(
        path=p, now=NOW, sync_status_path=_sync_status(tmp_path, NOW))
    assert level == 'ok' and '無新增' in msg
