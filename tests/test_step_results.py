"""計數由產生端寫成資料，而不是消費端從日誌人話裡撈。

2026-09-30 查出 run_weekly_sync.sh 的三個 grep 計數都靜默失準：

- `SYNCED_COUNT` 比對「新增 N 個 PDF」，程式印的是「更新完成: 新增 N 個」→ 永遠 0。
  累計 119 次執行 total_synced_pdfs 都是 0，而 9/30 那輪實際新增 2,499 份。
- `UPLOADED_COUNT` 比對「報告上傳成功」——那是**後端回應文字**被原樣印出，
  對方改字就歸零。
- `FAILED_COUNT` 用 grep -c「上傳失敗」，同時數到上傳錯誤、同步 adapter 的逐檔
  失敗、週報的「附件上傳失敗」。

核心不變式：**讀不到一律 None（未取得），不可回 0。** 回 0 就是把量測失敗偽裝
成正常結果，那正是這個 bug 活了 119 輪還沒人發現的原因。
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync import step_results as sr
from geobingan_sync.steps.record_sync_result import read_counts

NOW = datetime(2026, 9, 30, 10, 0)
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------- 寫入／讀取 ----------

def test_write_then_read(tmp_path):
    sr.write(sr.SYNC, {'synced': 2499}, now=NOW, base=str(tmp_path))
    d = sr.read(sr.SYNC, base=str(tmp_path))
    assert d['synced'] == 2499 and d['generated_at'] == NOW.isoformat()


def test_missing_file_is_none_not_zero(tmp_path):
    """最重要的一條：沒量到不是 0。"""
    assert sr.read(sr.SYNC, base=str(tmp_path)) is None


@pytest.mark.parametrize('body', ['not json', '[]', '"x"', ''])
def test_corrupt_file_is_none(tmp_path, body):
    (tmp_path / 'step_result_sync.json').write_text(body, encoding='utf-8')
    assert sr.read(sr.SYNC, base=str(tmp_path)) is None


def test_stale_file_is_none(tmp_path):
    """上一輪留下的檔案不算。沿用會把舊數字報成今天的——錯的數字看起來像對的。"""
    sr.write(sr.SYNC, {'synced': 5}, now=NOW - timedelta(days=1), base=str(tmp_path))
    assert sr.read(sr.SYNC, not_before=NOW, base=str(tmp_path)) is None
    assert sr.read(sr.SYNC, base=str(tmp_path))['synced'] == 5, '不傳 not_before 就不檢查新鮮度'


def test_file_written_exactly_at_run_start_counts(tmp_path):
    sr.write(sr.SYNC, {'synced': 7}, now=NOW, base=str(tmp_path))
    assert sr.read(sr.SYNC, not_before=NOW, base=str(tmp_path))['synced'] == 7


def test_unparseable_generated_at_is_none(tmp_path):
    p = tmp_path / 'step_result_sync.json'
    p.write_text(json.dumps({'synced': 9, 'generated_at': 'xxx'}), encoding='utf-8')
    assert sr.read(sr.SYNC, not_before=NOW, base=str(tmp_path)) is None


def test_atomic_write_leaves_no_tmp(tmp_path):
    sr.write(sr.UPLOAD, {'uploaded': 1, 'failed': 0}, now=NOW, base=str(tmp_path))
    assert not list(tmp_path.glob('*.tmp'))


def test_unknown_step_raises():
    with pytest.raises(ValueError):
        sr.path_for('nope')


@pytest.mark.parametrize('raw,expected', [
    ('2026-09-30T10:00:00', datetime(2026, 9, 30, 10, 0)),
    ('', None),
    ('garbage', None),
])
def test_parse_run_started(raw, expected):
    assert sr.parse_run_started(raw) == expected


def test_parse_run_started_accepts_epoch():
    ts = int(NOW.timestamp())
    assert sr.parse_run_started(str(ts)) == NOW


# ---------- record_sync_result 的取數 ----------

def test_counts_from_both_files(tmp_path):
    sr.write(sr.SYNC, {'synced': 2499}, now=NOW, base=str(tmp_path))
    sr.write(sr.UPLOAD, {'uploaded': 15, 'failed': 2}, now=NOW, base=str(tmp_path))
    assert read_counts(run_started=NOW.isoformat(), base=str(tmp_path)) == (2499, 15, 2)


def test_missing_sync_file_gives_none_for_synced(tmp_path):
    sr.write(sr.UPLOAD, {'uploaded': 3, 'failed': 0}, now=NOW, base=str(tmp_path))
    assert read_counts(run_started=NOW.isoformat(), base=str(tmp_path)) == (None, 3, 0)


def test_missing_upload_file_gives_none_for_uploads(tmp_path):
    sr.write(sr.SYNC, {'synced': 8}, now=NOW, base=str(tmp_path))
    assert read_counts(run_started=NOW.isoformat(), base=str(tmp_path)) == (8, None, None)


def test_upload_skipped_is_a_measured_zero(tmp_path):
    """暫停上傳是**確定沒上傳**，回 0 才對；那是量到的 0，不是沒量到。"""
    sr.write(sr.SYNC, {'synced': 8}, now=NOW, base=str(tmp_path))
    assert read_counts(run_started=NOW.isoformat(), upload_skipped='paused',
                       base=str(tmp_path)) == (8, 0, 0)


def test_stale_files_give_none_not_last_run_numbers(tmp_path):
    sr.write(sr.SYNC, {'synced': 999}, now=NOW - timedelta(days=1), base=str(tmp_path))
    sr.write(sr.UPLOAD, {'uploaded': 99, 'failed': 9}, now=NOW - timedelta(days=1),
             base=str(tmp_path))
    assert read_counts(run_started=NOW.isoformat(), base=str(tmp_path)) == (None, None, None)


@pytest.mark.parametrize('raw', ['', 'garbage'])
def test_no_run_started_means_unavailable_not_stale_numbers(tmp_path, raw):
    """拿不到本輪起點就無法確認結果檔屬於本輪，一律回未取得。

    原本這條的理由寫「寧可用檔案內容，也別無謂丟掉」——那是錯的，而且違反本模組
    自己的原則：沿用上一輪的數字＝把昨天的報成今天的，「錯的數字看起來像對的」
    比「未取得」更糟。
    """
    sr.write(sr.SYNC, {'synced': 4}, now=NOW - timedelta(days=5), base=str(tmp_path))
    sr.write(sr.UPLOAD, {'uploaded': 9, 'failed': 1}, now=NOW - timedelta(days=5),
             base=str(tmp_path))
    assert read_counts(run_started=raw, base=str(tmp_path)) == (None, None, None)


# ---------- shell 不得再從日誌撈計數 ----------

def test_shell_no_longer_greps_counts_from_log():
    """守門：這正是壞掉的地方。任何人把 grep 計數加回來就該紅。"""
    src = open(os.path.join(REPO, 'run_weekly_sync.sh'), encoding='utf-8').read()
    for pat in ('報告上傳成功', 'SYNCED_COUNT', 'UPLOADED_COUNT', 'FAILED_COUNT',
                'grep -o "新增'):
        assert pat not in src, f'run_weekly_sync.sh 又開始從日誌撈計數: {pat}'


def test_shell_passes_run_started_and_skip_reason():
    src = open(os.path.join(REPO, 'run_weekly_sync.sh'), encoding='utf-8').read()
    assert 'SYNC_RUN_STARTED' in src and 'SYNC_UPLOAD_SKIPPED' in src
    assert 'UPLOAD_SKIPPED="paused"' in src, '暫停路徑要標成確定沒上傳'


def test_shell_syntax_is_valid():
    r = subprocess.run(['bash', '-n', os.path.join(REPO, 'run_weekly_sync.sh')],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------- adapter 逐檔失敗不可撞到「上傳失敗」語彙 ----------

def test_adapter_failure_message_does_not_say_upload_failed():
    """「上傳失敗」在本專案指 geoBingAn 後端上傳。混用會讓操作者查錯方向，
    而舊 shell 還曾用它 grep 計數。

    只掃**會被印出來的那幾行**。第一版我掃整個檔案，結果被自己解釋這件事的
    註解打中——掃原始碼的守門測試要把範圍縮到真正在意的區塊。
    """
    path = os.path.join(REPO, 'geobingan_sync/steps/sync_permits.py')
    printed = [ln for ln in open(path, encoding='utf-8')
               if ('_print(' in ln or 'print(' in ln) and not ln.lstrip().startswith('#')]
    assert printed, '前提：這個檔案有印出訊息'
    offenders = [ln.strip() for ln in printed if '上傳失敗' in ln]
    assert not offenders, f'同步步驟印出了「上傳失敗」字樣: {offenders}'
    assert any('寫入目標資料夾失敗' in ln for ln in printed), '前提：改用了新的措辭'


# ---------- P1：時區格式不一致不可讓記錄整輪崩潰 ----------
#
# shell 的 `date -Iseconds` 在 macOS 回帶時區的 2026-09-30T11:50:32+08:00，
# 結果檔用 datetime.now() 寫的是 naive。直接相比會拋
# TypeError: can't compare offset-naive and offset-aware datetimes，
# 而那個例外發生在 record_sync_result 裡——整輪狀態就記不下來，比計數錯更嚴重。

from datetime import timezone

#: **本機**的 UTC 位移。naive 時間在本專案一律指本地時間，所以要構造「同一瞬間
#: 的 aware 版本」必須用本機位移，不能寫死 +08:00——CI 跑 UTC，寫死就會變成
#: 在比兩個不同的瞬間，測試於是依賴執行機器的時區（本機綠、CI 紅）。
LOCAL_TZ = datetime.now().astimezone().tzinfo
OTHER_TZ = timezone(timedelta(hours=8 if datetime.now().astimezone().utcoffset()
                              != timedelta(hours=8) else -5))


def _local_aware(dt):
    """同一個本地時刻的 aware 版本。"""
    return dt.replace(tzinfo=LOCAL_TZ)


def test_aware_run_started_against_naive_file_does_not_raise(tmp_path):
    sr.write(sr.SYNC, {'synced': 5}, now=NOW, base=str(tmp_path))
    d = sr.read(sr.SYNC, not_before=_local_aware(NOW), base=str(tmp_path))
    assert d is not None and d['synced'] == 5


def test_aware_file_against_naive_run_started_does_not_raise(tmp_path):
    p = tmp_path / 'step_result_sync.json'
    p.write_text(json.dumps({'synced': 7,
                             'generated_at': _local_aware(NOW).isoformat()}),
                 encoding='utf-8')
    assert sr.read(sr.SYNC, not_before=NOW, base=str(tmp_path))['synced'] == 7


def test_aware_stale_file_is_still_detected_as_stale(tmp_path):
    """統一時區不能順手把新鮮度檢查弄丟。"""
    p = tmp_path / 'step_result_sync.json'
    p.write_text(json.dumps({'synced': 9,
                             'generated_at': _local_aware(NOW - timedelta(days=1)).isoformat()}),
                 encoding='utf-8')
    assert sr.read(sr.SYNC, not_before=NOW, base=str(tmp_path)) is None


def test_comparison_is_by_instant_not_by_wall_clock(tmp_path):
    """換算後比的是**瞬間**，不是牆上時鐘的數字。

    檔案標 10:00+08:00、本輪開始標 10:00 本地時間：只有本機剛好是 +08:00 時兩者
    才是同一瞬間。刻意用另一個位移，斷言判斷依據是實際時刻。
    """
    p = tmp_path / 'step_result_sync.json'
    far_future = (NOW + timedelta(days=1)).replace(tzinfo=OTHER_TZ)
    p.write_text(json.dumps({'synced': 3, 'generated_at': far_future.isoformat()}),
                 encoding='utf-8')
    assert sr.read(sr.SYNC, not_before=NOW, base=str(tmp_path))['synced'] == 3, \
        '明日的時刻無論哪個位移都不該被判成過期'
    long_past = (NOW - timedelta(days=30)).replace(tzinfo=OTHER_TZ)
    p.write_text(json.dumps({'synced': 4, 'generated_at': long_past.isoformat()}),
                 encoding='utf-8')
    assert sr.read(sr.SYNC, not_before=NOW, base=str(tmp_path)) is None


@pytest.mark.parametrize('raw', ['2026-09-30T10:00:00+08:00', '2026-09-30T10:00:00'])
def test_parse_run_started_normalises_to_naive(raw):
    got = sr.parse_run_started(raw)
    assert got is not None and got.tzinfo is None


def test_shell_iso_format_round_trips(tmp_path):
    """用 shell 實際產生的字串跑一次，不要只用手寫的假值。"""
    raw = subprocess.run(['bash', '-c', 'date -Iseconds 2>/dev/null || date +%Y-%m-%dT%H:%M:%S'],
                         capture_output=True, text=True).stdout.strip()
    started = sr.parse_run_started(raw)
    assert started is not None and started.tzinfo is None
    sr.write(sr.SYNC, {'synced': 1}, base=str(tmp_path))          # now()＝naive
    assert sr.read(sr.SYNC, not_before=started, base=str(tmp_path)) is not None


def test_read_counts_with_aware_run_started(tmp_path):
    """整條路徑：record_sync_result 收到帶時區的字串也要正常取數。"""
    sr.write(sr.SYNC, {'synced': 2499}, now=NOW, base=str(tmp_path))
    sr.write(sr.UPLOAD, {'uploaded': 15, 'failed': 2}, now=NOW, base=str(tmp_path))
    assert read_counts(run_started=_local_aware(NOW).isoformat(),
                       base=str(tmp_path)) == (2499, 15, 2)


# ---------- P2：確定沒上傳是量到的 0，不是未取得 ----------

def _upload_main_early_exit(tmp_path, monkeypatch, note):
    """直接測 _record_upload_result：它是所有早退點共用的那一支。"""
    import geobingan_sync.steps.upload_pdfs as up
    monkeypatch.setenv('SYNC_RUN_STARTED', NOW.isoformat())
    monkeypatch.setattr(sr, '_BASE', str(tmp_path))
    up._record_upload_result(0, 0, note=note)
    return sr.read(sr.UPLOAD, base=str(tmp_path))


@pytest.mark.parametrize('note', ['nothing_to_upload', 'budget_blocked', 'cancelled'])
def test_determinate_zero_is_recorded(tmp_path, monkeypatch, note):
    d = _upload_main_early_exit(tmp_path, monkeypatch, note)
    assert d is not None, '確定沒上傳也要留下紀錄，否則摘要會報「未取得」'
    assert d['uploaded'] == 0 and d['failed'] == 0 and d['note'] == note


def test_early_exit_paths_all_record_a_result():
    """守門：main() 裡每個「確定沒上傳」的 sys.exit 前面都要有紀錄。

    只修被指出的那一個不夠——同形態的其他早退也會落回「未取得」。

    只掃 main() 函式本體。`__main__` 的 KeyboardInterrupt 不算：中斷時可能已經
    上傳了幾份，我們**確實不知道**數量，報「未取得」才對。第一版掃整個檔案就被
    那段打中——掃原始碼的守門測試要把範圍縮到真正在意的區塊。
    """
    lines = open(os.path.join(REPO, 'geobingan_sync/steps/upload_pdfs.py'),
                 encoding='utf-8').read().split('\n')
    start = next(i for i, ln in enumerate(lines) if ln.startswith('def main('))
    end = next((i for i, ln in enumerate(lines[start + 1:], start + 1)
                if ln.startswith('def ') or ln.startswith("if __name__")), len(lines))
    body = lines[start:end]
    exits = [(i, ln) for i, ln in enumerate(body)
             if 'sys.exit(0)' in ln or 'sys.exit(3)' in ln]
    assert exits, '前提：main() 裡有確定沒上傳的早退'
    for i, ln in exits:
        window = '\n'.join(body[max(0, i - 4):i])
        assert '_record_upload_result' in window, (
            f'main() 第 {start + i + 1} 行的 {ln.strip()} 之前沒有寫出上傳計數')


# ---------- 多城市：同一輪內累加，不是後者覆蓋前者 ----------

def test_accumulate_adds_within_the_same_run(tmp_path):
    sr.accumulate(sr.SYNC, {'synced': 10, 'permits_with_new': 2}, now=NOW,
                  base=str(tmp_path), run_started=NOW.isoformat())
    sr.accumulate(sr.SYNC, {'synced': 5, 'permits_with_new': 1}, now=NOW,
                  base=str(tmp_path), run_started=NOW.isoformat())
    d = sr.read(sr.SYNC, base=str(tmp_path))
    assert d['synced'] == 15 and d['permits_with_new'] == 3


def test_accumulate_ignores_previous_run(tmp_path):
    """跨輪不可累加到舊數字。"""
    sr.write(sr.SYNC, {'synced': 999}, now=NOW - timedelta(days=1), base=str(tmp_path))
    sr.accumulate(sr.SYNC, {'synced': 10}, now=NOW, base=str(tmp_path),
                  run_started=NOW.isoformat())
    assert sr.read(sr.SYNC, base=str(tmp_path))['synced'] == 10


def test_accumulate_keeps_latest_note(tmp_path):
    sr.accumulate(sr.UPLOAD, {'uploaded': 0, 'failed': 0, 'note': 'a'}, now=NOW,
                  base=str(tmp_path), run_started=NOW.isoformat())
    sr.accumulate(sr.UPLOAD, {'uploaded': 3, 'failed': 0, 'note': 'b'}, now=NOW,
                  base=str(tmp_path), run_started=NOW.isoformat())
    d = sr.read(sr.UPLOAD, base=str(tmp_path))
    assert d['uploaded'] == 3 and d['note'] == 'b'


def test_producers_use_accumulate_not_write():
    """守門：產生端若改回 write，多城市就會互相覆蓋。"""
    for rel in ('geobingan_sync/steps/sync_permits.py', 'geobingan_sync/steps/upload_pdfs.py'):
        src = open(os.path.join(REPO, rel), encoding='utf-8').read()
        assert 'step_results.write(' not in src, f'{rel} 應改用 accumulate'
        assert 'step_results.accumulate(' in src


# ---------- P2：邊界不可放棄，否則跨輪累加（review 第三輪）----------
#
# accumulate 原本在拿不到 SYNC_RUN_STARTED 時用 read(not_before=None)，
# 那是「不檢查新鮮度」＝fail-open。直接執行 CLI 就會把上一輪的數字繼續加上去：
# 實測昨天 synced=100、本輪新增 2，寫成 102。

def test_accumulate_without_boundary_does_not_carry_over(tmp_path, monkeypatch):
    """核心回歸：沒有 SYNC_RUN_STARTED 時也不可累加到上一輪。"""
    monkeypatch.delenv('SYNC_RUN_STARTED', raising=False)
    sr.write(sr.SYNC, {'synced': 100}, now=datetime.now() - timedelta(days=1),
             base=str(tmp_path))
    sr.accumulate(sr.SYNC, {'synced': 2}, base=str(tmp_path))
    assert sr.read(sr.SYNC, base=str(tmp_path))['synced'] == 2, '不可變成 102'


def test_accumulate_without_boundary_still_adds_within_one_process(tmp_path, monkeypatch):
    """城市迴歸跑在同一行程，同輪仍要相加——備援邊界不能把這件事也擋掉。"""
    monkeypatch.delenv('SYNC_RUN_STARTED', raising=False)
    sr.accumulate(sr.SYNC, {'synced': 2}, base=str(tmp_path))
    sr.accumulate(sr.SYNC, {'synced': 3}, base=str(tmp_path))
    assert sr.read(sr.SYNC, base=str(tmp_path))['synced'] == 5


@pytest.mark.parametrize('raw', ['', 'garbage'])
def test_accumulate_with_invalid_boundary_does_not_carry_over(tmp_path, monkeypatch, raw):
    monkeypatch.setenv('SYNC_RUN_STARTED', raw)
    sr.write(sr.UPLOAD, {'uploaded': 50, 'failed': 5},
             now=datetime.now() - timedelta(days=2), base=str(tmp_path))
    sr.accumulate(sr.UPLOAD, {'uploaded': 1, 'failed': 0}, base=str(tmp_path))
    d = sr.read(sr.UPLOAD, base=str(tmp_path))
    assert (d['uploaded'], d['failed']) == (1, 0)


def test_accumulate_never_reads_with_no_boundary():
    """守門：accumulate 不可再把 None 邊界直接餵給 read。"""
    src = open(os.path.join(REPO, 'geobingan_sync/step_results.py'), encoding='utf-8').read()
    body = src[src.index('def accumulate('):]
    assert '_PROCESS_STARTED' in body, 'accumulate 必須有邊界備援'
    assert 'if started is None:' in body
