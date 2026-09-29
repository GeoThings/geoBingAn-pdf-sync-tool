"""整體執行時間看門狗的行為測試（PR #98 review 兩個 P1）。

2026-09-27 一次同步跑了 734.6 分鐘（12.2 小時）：Drive 掃描 51 次 Connection reset，
每次請求各自重試但整體沒有上限，壓到隔天排程、失敗通知隔天才到。

這裡 source **正式的** lib/watchdog.sh 跑真流程，而不是在測試裡抄一份邏輯——
抄的那份會和正式的走鐘，等於沒測。
"""
import os
import subprocess
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, 'tests', 'fixtures', 'watchdog_harness.sh')


def _run(tmp_path, **env):
    e = dict(os.environ, LOG_DIR=str(tmp_path))
    e.update({k: str(v) for k, v in env.items()})
    p = subprocess.run(['bash', HARNESS], env=e, capture_output=True, text=True, timeout=180)
    events = (tmp_path / 'events').read_text(encoding='utf-8') if (tmp_path / 'events').exists() else ''
    return p, events


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_normal_completion_exits_zero(tmp_path):
    p, events = _run(tmp_path, MAX_RUNTIME_SECONDS=20, WORK_SECONDS=1)
    assert p.returncode == 0
    assert 'CLEANUP_DONE err=0' in events


def test_timeout_exits_one_with_timeout_reason(tmp_path):
    """超時要回確定的 1，並用獨立的錯誤訊息（不可混進「未預期的錯誤」）。"""
    p, events = _run(tmp_path, MAX_RUNTIME_SECONDS=2, WORK_SECONDS=60)
    assert p.returncode == 1, p.returncode
    assert 'CLEANUP_DONE err=1 msg=TIMEOUT' in events
    assert '已達執行時間上限' in (tmp_path / 'harness.log').read_text(encoding='utf-8')


def test_timeout_leaves_no_orphan_children(tmp_path):
    """review P1-1：只殺主 shell 的話，子程序會變 orphan 繼續跑、繼續寫狀態。"""
    p, _ = _run(tmp_path, MAX_RUNTIME_SECONDS=2, WORK_SECONDS=60, TIMEOUT_GRACE_SECONDS=3)
    assert p.returncode == 1
    time.sleep(1)
    for name in ('main.pid', 'wrapper.pid', 'child.pid'):
        f = tmp_path / name
        if not f.exists():
            continue
        pid = int(f.read_text().strip())
        assert not _alive(pid), f'{name}={pid} 仍然存活（orphan）'


def test_cleanup_exceeding_grace_is_force_killed(tmp_path):
    """review P1-2：cleanup 卡住時，宣稱的 grace deadline 必須真的存在。

    cleanup 故意睡 30 秒、寬限只有 3 秒 → 看門狗必須在寬限後 SIGKILL，
    所以 CLEANUP_DONE 永遠不會寫出來。若 cleanup 一開始就取消看門狗，
    這裡會等到 cleanup 自己睡完、測試就會看到 CLEANUP_DONE。
    """
    p, events = _run(tmp_path, MAX_RUNTIME_SECONDS=2, WORK_SECONDS=60,
                     TIMEOUT_GRACE_SECONDS=3, CLEANUP_HANG_SECONDS=30)
    assert 'CLEANUP_START' in events, 'cleanup 應該有被觸發'
    assert 'CLEANUP_DONE' not in events, 'cleanup 卡住卻沒被強制終止＝grace 是假的'
    assert p.returncode != 0


def test_watchdog_disabled_when_zero(tmp_path):
    p, events = _run(tmp_path, MAX_RUNTIME_SECONDS=0, WORK_SECONDS=1)
    assert p.returncode == 0 and 'CLEANUP_DONE err=0' in events
    assert not (tmp_path / 'harness.log').exists() or \
        '已達執行時間上限' not in (tmp_path / 'harness.log').read_text(encoding='utf-8')


def test_sync_script_sources_the_shared_watchdog():
    """正式腳本必須 source lib/watchdog.sh，不可自己抄一份。"""
    src = open(os.path.join(ROOT, 'run_weekly_sync.sh'), encoding='utf-8').read()
    assert 'source "$SCRIPT_DIR/lib/watchdog.sh"' in src
    assert 'wd_start' in src and 'wd_stop' in src
    # 超時路徑不可取消看門狗
    assert 'if [ "${WAS_TIMEOUT}" -eq 0 ]; then' in src
