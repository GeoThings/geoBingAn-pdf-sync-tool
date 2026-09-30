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


def test_no_run_started_skips_freshness_check(tmp_path):
    """傳不到開始時間時不要因此全部變未取得——寧可用檔案內容，也別無謂丟掉。"""
    sr.write(sr.SYNC, {'synced': 4}, now=NOW - timedelta(days=5), base=str(tmp_path))
    assert read_counts(run_started='', base=str(tmp_path))[0] == 4


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
