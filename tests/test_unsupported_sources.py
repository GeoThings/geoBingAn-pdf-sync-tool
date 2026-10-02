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
    """同步本身停到 check_last_sync 會報時，這裡不重複告警。

    「會不會報」直接問 check_last_sync，不複製門檻——複製過就出過事（見下方
    test_no_alerting_vacuum_between_thresholds）。
    """
    p = _sync_status(tmp_path, NOW - timedelta(days=30))
    assert health_check.check_last_sync(path=p, now=NOW)[0] != 'ok', '前提：同步檢查確實會亮'
    level, msg = health_check.check_unsupported_sources(
        path=str(tmp_path / 'nope.json'), now=NOW, sync_status_path=p)
    assert level == 'ok' and '已另行告警' in msg


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


# ---------- 兩個檢查之間不可有告警真空（re-review P2）----------
#
# 早先版本用自己的 48 小時門檻抑制告警，說「交給 check_last_sync」——但那支要
# 超過 10 天才報。最後一次成功同步在 3～10 天前而名單缺失時，**兩邊同時綠燈**。
# 修法是不複製門檻：直接呼叫 check_last_sync，只有它真的會亮才讓給它。

@pytest.mark.parametrize('days', [3, 10])
def test_missing_list_still_alerts_between_thresholds(tmp_path, days):
    """reviewer 指定的兩個點：最後同步 3 天／10 天前且成功，名單缺失。"""
    p = _sync_status(tmp_path, NOW - timedelta(days=days))
    assert health_check.check_last_sync(path=p, now=NOW)[0] == 'ok', '前提：同步檢查此時不會亮'
    level, msg = health_check.check_unsupported_sources(
        path=str(tmp_path / 'nope.json'), now=NOW, sync_status_path=p)
    assert level == 'warning' and '找不到' in msg, msg


@pytest.mark.parametrize('days', list(range(0, 31)))
def test_no_alerting_vacuum_between_thresholds(tmp_path, days):
    """掃過整段區間：名單缺失時，兩個燈至少要亮一個。

    點測試只證明選中的那兩格沒事；門檻一旦再度分叉，真空會出現在沒測到的那格。
    """
    p = _sync_status(tmp_path, NOW - timedelta(days=days))
    sync_level, _ = health_check.check_last_sync(path=p, now=NOW)
    uns_level, _ = health_check.check_unsupported_sources(
        path=str(tmp_path / 'nope.json'), now=NOW, sync_status_path=p)
    assert 'ok' != sync_level or 'ok' != uns_level, (
        f'最後同步 {days} 天前、名單缺失，兩個檢查卻都是綠燈')


def test_failed_last_sync_also_suppresses(tmp_path):
    """check_last_sync 也會因『上次同步失敗』亮燈，抑制條件要跟著涵蓋。"""
    p = tmp_path / 'ss.json'
    p.write_text(json.dumps({'last_run': (NOW - timedelta(hours=2)).isoformat(),
                             'last_status': 'failure'}), encoding='utf-8')
    assert health_check.check_last_sync(path=str(p), now=NOW)[0] != 'ok'
    level, msg = health_check.check_unsupported_sources(
        path=str(tmp_path / 'nope.json'), now=NOW, sync_status_path=str(p))
    assert level == 'ok' and '已另行告警' in msg


def test_stale_list_alerts_regardless_of_sync_state(tmp_path):
    """名單過期是名單自己的問題，不因同步也在告警而被吞掉。"""
    old = us.build(_entries(), now=NOW - timedelta(days=30))
    p = _write(tmp_path, _entries(), now=NOW - timedelta(days=5), previous=old)
    level, msg = health_check.check_unsupported_sources(
        path=p, now=NOW, sync_status_path=_sync_status(tmp_path, NOW - timedelta(days=30)))
    assert level == 'warning' and '未更新' in msg


# ---------- 定期實測：把「需登入」從手寫常數換成測出來的 ----------
#
# FAMILY_FINDINGS 是 2026-09-29 人工探測後**手寫**的結論，會過期：承造人改好了權限
# 我們不會知道（同 #100 失效資料夾永不重驗的那個錯）。改成定期實測。
#
# ⚠️ 自動探測刻意只認 **URL 層級**證據。9/29 我用 body 關鍵字判斷，把 Dropbox 與
# Synology gofile 都判錯——那些頁面本來就含 signin 字串，是 JS 外殼不是登入牆，
# 最後要用真實瀏覽器才分得出來。所以 HTTP 200 一律是 reachable_unknown，不下結論。

from geobingan_sync.unsupported_sources import (PROBE_INTERVAL_DAYS, VERDICT_AUTH,
                                                VERDICT_UNKNOWN, VERDICT_UNREACHABLE,
                                                due_for_probe, probe_due, probe_once)


def _fetch(*responses):
    """依序回傳 (status, location, body)。"""
    seq = list(responses)
    calls = []

    def _f(url):
        calls.append(url)
        return seq.pop(0) if seq else (200, '', '')
    _f.calls = calls
    return _f


@pytest.mark.parametrize('location,marker', [
    ('/_forms/default.aspx?ReturnUrl=%2fx', '/_forms/default.aspx'),
    ('https://x.sharepoint.com/_layouts/15/sharepointerror.aspx?scenario=Y', 'sharepointerror.aspx'),
    ('https://accounts.google.com/v3/signin/identifier?x=1', 'accounts.google.com/v3/signin'),
    ('https://login.microsoftonline.com/common/oauth2', 'login.microsoftonline.com'),
])
def test_auth_redirect_is_detected(location, marker):
    v, note = probe_once('https://x.test/a', fetch=_fetch((302, location, '')))
    assert v == VERDICT_AUTH and note == marker


def test_relative_auth_redirect_is_detected():
    """SharePoint 回的是相對路徑，一樣要認得出來。

    注意：marker 是子字串比對，相對路徑本身就含得到，所以這條**不是**在驗
    urljoin（原本的測試名稱寫錯了，注回「拿掉 urljoin」時它照樣綠）。
    urljoin 真正的作用見下一條。
    """
    v, _ = probe_once('https://x.sharepoint.com/:f:/g/p/E1',
                      fetch=_fetch((302, '/_forms/default.aspx', '')))
    assert v == VERDICT_AUTH


def test_relative_redirect_is_resolved_before_the_next_hop():
    """urljoin 的真正作用：讓**下一跳**拿到絕對網址。

    不 urljoin 的話 current 會變成 '/step2'，下一次請求就不是合法網址。
    這條是注回驗證逼出來的——原本沒有任何測試擋得住拿掉 urljoin。
    """
    f = _fetch((302, '/step2', ''), (200, '', 'ok'))
    v, _ = probe_once('https://x.test/dir/a', fetch=f)
    assert v == VERDICT_UNKNOWN
    assert f.calls == ['https://x.test/dir/a', 'https://x.test/step2'], \
        f'第二跳應收到絕對網址，實際 {f.calls}'


def test_protocol_relative_redirect_is_resolved():
    f = _fetch((302, '//other.test/x', ''), (200, '', 'ok'))
    probe_once('https://x.test/a', fetch=f)
    assert f.calls[1] == 'https://other.test/x'


def test_http_200_is_never_claimed_as_needing_login():
    """核心：JS 外殼也是 200。9/29 用 body 關鍵字判斷把 Dropbox／gofile 判錯。"""
    body = '<html>...signin...login...Access via Synology...</html>'
    v, note = probe_once('https://gofile.me/a/b', fetch=_fetch((200, '', body)))
    assert v == VERDICT_UNKNOWN and note == 'http_200'


@pytest.mark.parametrize('status', [404, 403, 500, 503])
def test_non_200_is_unreachable(status):
    v, note = probe_once('https://x.test/a', fetch=_fetch((status, '', '')))
    assert v == VERDICT_UNREACHABLE and note == f'http_{status}'


def test_connection_error_is_unreachable():
    def _boom(_u):
        raise TimeoutError('slow')
    v, note = probe_once('http://1.2.3.4:5000/a', fetch=_boom)
    assert v == VERDICT_UNREACHABLE and note == 'TimeoutError'


def test_redirect_chain_is_followed_then_resolved():
    f = _fetch((302, 'https://a.test/1', ''), (302, 'https://a.test/2', ''), (200, '', 'ok'))
    v, _ = probe_once('https://x.test/a', fetch=f)
    assert v == VERDICT_UNKNOWN and len(f.calls) == 3


def test_redirect_loop_is_bounded():
    def _loop(_u):
        return (302, 'https://x.test/again', '')
    v, note = probe_once('https://x.test/a', fetch=_loop)
    assert v == VERDICT_UNREACHABLE and 'too_many_hops' in note


# ---------- 重測間隔 ----------

def test_never_probed_is_due():
    assert due_for_probe({}, now=NOW) is True


def test_not_due_within_interval():
    info = {'probed_at': (NOW - timedelta(days=PROBE_INTERVAL_DAYS - 1)).isoformat()}
    assert due_for_probe(info, now=NOW) is False


def test_due_after_interval():
    info = {'probed_at': (NOW - timedelta(days=PROBE_INTERVAL_DAYS)).isoformat()}
    assert due_for_probe(info, now=NOW) is True


@pytest.mark.parametrize('bad', ['', 'not-a-date', None, 12345])
def test_corrupt_probe_timestamp_falls_open_to_probing(bad):
    """髒資料不可以讓某一案永遠不重測。"""
    assert due_for_probe({'probed_at': bad}, now=NOW) is True


# ---------- probe_due 與恢復偵測 ----------

def _data(**verdicts):
    d = us.build([(p, f'https://{p}.test/x', '') for p in verdicts], now=NOW)
    for p, v in verdicts.items():
        if v:
            d['sources'][p]['probe_verdict'] = v
            d['sources'][p]['probed_at'] = (NOW - timedelta(days=30)).isoformat()
    return d


def test_probe_due_records_verdict_and_timestamp():
    d = _data(A=None)
    n, rec = probe_due(d, now=NOW, fetch=_fetch((302, '/_forms/default.aspx', '')))
    info = d['sources']['A']
    assert (n, rec) == (1, [])
    assert info['probe_verdict'] == VERDICT_AUTH
    assert info['probed_at'] == NOW.isoformat()


def test_recovery_from_auth_is_reported():
    """先前要登入、現在回 200 → 對方可能改好權限，必須出聲。"""
    d = _data(A=VERDICT_AUTH)
    n, rec = probe_due(d, now=NOW, fetch=_fetch((200, '', '<html>x</html>')))
    assert (n, rec) == (1, ['A'])
    assert d['sources']['A']['probe_recovered_at'] == NOW.isoformat()


def test_recovery_from_unreachable_is_reported():
    d = _data(A=VERDICT_UNREACHABLE)
    _n, rec = probe_due(d, now=NOW, fetch=_fetch((200, '', 'x')))
    assert rec == ['A']


def test_still_auth_is_not_a_recovery():
    d = _data(A=VERDICT_AUTH)
    _n, rec = probe_due(d, now=NOW, fetch=_fetch((302, '/_forms/default.aspx', '')))
    assert rec == [] and 'probe_recovered_at' not in d['sources']['A']


def test_first_probe_returning_200_is_not_a_recovery():
    """沒有先前結論就談不上恢復——否則第一輪會把整份名單報成好消息。"""
    d = _data(A=None)
    _n, rec = probe_due(d, now=NOW, fetch=_fetch((200, '', 'x')))
    assert rec == []


def test_probe_due_skips_fresh_entries():
    d = _data(A=VERDICT_AUTH)
    d['sources']['A']['probed_at'] = NOW.isoformat()

    def _never(_u):
        raise AssertionError('未到間隔不該重測')
    n, rec = probe_due(d, now=NOW, fetch=_never)
    assert (n, rec) == (0, [])


def test_max_probes_bounds_one_round():
    d = _data(**{f'P{i}': VERDICT_AUTH for i in range(6)})
    n, _rec = probe_due(d, now=NOW, fetch=lambda u: (200, '', 'x'), max_probes=2)
    assert n == 2


# ---------- 探測結果跟著連結走 ----------

def test_probe_result_is_kept_when_url_unchanged():
    d = _data(A=VERDICT_AUTH)
    again = us.build([('A', 'https://A.test/x', '')], previous=d, now=NOW)
    assert again['sources']['A']['probe_verdict'] == VERDICT_AUTH


def test_probe_result_is_dropped_when_url_changes():
    """舊結論套到新連結＝憑空宣稱沒測過的事（同 #100 快取鍵綁實體 ID 的教訓）。"""
    d = _data(A=VERDICT_AUTH)
    again = us.build([('A', 'https://A.test/CHANGED', '')], previous=d, now=NOW)
    assert 'probe_verdict' not in again['sources']['A']
    assert due_for_probe(again['sources']['A'], now=NOW) is True


# ---------- 彙總以實測為主 ----------

def test_summarise_prefers_measured_verdict_over_handwritten():
    d = us.build([('A', 'https://x-my.sharepoint.com/a', '')], now=NOW)
    d['sources']['A'].update({'probe_verdict': VERDICT_UNKNOWN,
                              'probed_at': NOW.isoformat()})
    fam, n, status, date, _ = us.summarise(d)[0]
    assert fam == 'SharePoint' and n == 1
    assert status == '可開啟但未支援，實測 1/1', '實測要蓋掉手寫的「需登入」'
    assert date == NOW.strftime('%Y-%m-%d')


def test_summarise_marks_mixed_verdicts_within_a_family():
    d = us.build([('A', 'https://a-my.sharepoint.com/a', ''),
                  ('B', 'https://b-my.sharepoint.com/b', '')], now=NOW)
    d['sources']['A'].update({'probe_verdict': VERDICT_AUTH, 'probed_at': NOW.isoformat()})
    d['sources']['B'].update({'probe_verdict': VERDICT_UNKNOWN, 'probed_at': NOW.isoformat()})
    _fam, n, status, _date, _ = us.summarise(d)[0]
    assert n == 2
    assert status == '混合：需登入 1、可開啟但未支援 1，實測 2/2', \
        '不一致要逐項列案數，家族只是分類不是證據單位'


def test_summarise_falls_back_to_handwritten_when_never_probed():
    d = us.build([('A', 'https://x-my.sharepoint.com/a', '')], now=NOW)
    _fam, _n, status, date, _ = us.summarise(d)[0]
    assert status == '需登入' and date == '2026-09-29', '沒測過才用手寫結論'


# ---------- review P2①：部分實測不可擴張成家族結論 ----------

def test_summarise_does_not_extend_partial_probe_to_whole_family():
    """2 案裡只測了 1 案，不可印成「2 案需登入（實測）」。

    counts 是家族全體、verdicts 只有測過的，兩者不可混用。這正是這支程式要修掉
    的那個錯（手寫結論以家族為粒度），在彙總層又犯一次就等於沒修。
    """
    d = us.build([('A', 'https://a-my.sharepoint.com/a', ''),
                  ('B', 'https://b-my.sharepoint.com/b', '')], now=NOW)
    d['sources']['A'].update({'probe_verdict': VERDICT_AUTH, 'probed_at': NOW.isoformat()})
    _fam, n, status, _date, _ = us.summarise(d)[0]
    assert n == 2
    assert status == '需登入，實測 1/2', f'不可宣稱兩案都實測過: {status}'


def test_summarise_coverage_fraction_distinguishes_full_from_sampled():
    """16/16 與 1/16 必須長得不一樣，否則讀的人分不出全測與抽驗。"""
    full = us.build([(f'P{i}', f'https://h{i}-my.sharepoint.com/a', '') for i in range(4)],
                    now=NOW)
    for info in full['sources'].values():
        info.update({'probe_verdict': VERDICT_AUTH, 'probed_at': NOW.isoformat()})
    sampled = us.build([(f'P{i}', f'https://h{i}-my.sharepoint.com/a', '') for i in range(4)],
                       now=NOW)
    sampled['sources']['P0'].update({'probe_verdict': VERDICT_AUTH,
                                     'probed_at': NOW.isoformat()})
    assert us.summarise(full)[0][2] == '需登入，實測 4/4'
    assert us.summarise(sampled)[0][2] == '需登入，實測 1/4'


def test_summarise_mixed_partial_also_reports_coverage():
    """混合 + 部分實測：逐項案數與涵蓋率都要在。"""
    d = us.build([(f'P{i}', f'https://{i}.1.2.3/a', '') for i in range(4)], now=NOW)
    d['sources']['P0'].update({'probe_verdict': VERDICT_UNREACHABLE,
                               'probed_at': NOW.isoformat()})
    d['sources']['P1'].update({'probe_verdict': VERDICT_UNREACHABLE,
                               'probed_at': NOW.isoformat()})
    d['sources']['P2'].update({'probe_verdict': VERDICT_UNKNOWN,
                               'probed_at': NOW.isoformat()})
    _fam, n, status, _date, _ = us.summarise(d)[0]
    assert n == 4
    assert status == '混合：連不上 2、可開啟但未支援 1，實測 3/4'


# ---------- review P2②：未來時間戳不可永久延後重測 ----------

def test_future_probe_timestamp_falls_open_to_probing():
    """2099-01-01 可以解析，但不可能是真的觀測時間。

    now - last 會是負數 → 永遠 < interval → 這一案到 2099 年都不再探測。
    時鐘跳動或手動改壞資料都會踩到，必須跟毀損同樣 fail-open。
    """
    assert due_for_probe({'probed_at': '2099-01-01T00:00:00'}, now=NOW) is True
    assert due_for_probe({'probed_at': (NOW + timedelta(seconds=1)).isoformat()},
                         now=NOW) is True
    assert due_for_probe({'probed_at': (NOW + timedelta(days=365 * 73)).isoformat()},
                         now=NOW) is True


def test_future_timestamp_is_actually_reprobed_and_timestamp_repaired():
    """不只 predicate——probe_due 真的要重測，並把壞時間戳寫回正常值。"""
    d = us.build([('A', 'https://x-my.sharepoint.com/a', '')], now=NOW)
    d['sources']['A'].update({'probe_verdict': VERDICT_AUTH,
                              'probed_at': '2099-01-01T00:00:00'})
    probed, _recovered = us.probe_due(
        d, now=NOW, fetch=_fetch((302, 'https://x.test/_forms/default.aspx', '')))
    assert probed == 1, '未來時間戳必須被重測'
    assert d['sources']['A']['probed_at'] == NOW.isoformat(), '壞時間戳要被修回來'


def test_timestamp_exactly_now_is_not_reprobed_twice_in_one_run():
    """修①不可反過來把同一輪剛測完的案子又測一次（probed_at == now 不是未來）。"""
    info = {'probed_at': NOW.isoformat()}
    assert due_for_probe(info, now=NOW) is False
