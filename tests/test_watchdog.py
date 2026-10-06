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


# ---------- 2026-10-06：看門狗量的是「清醒時間」，START_TIME 量的是牆上時間 ----------
#
# 當天牆上 63.5 分鐘的執行裡，機器睡了 49.2 分鐘（6 段，最長 17 分），實際清醒只有
# 14.3 分鐘。`sleep 3600` 在系統睡眠期間不前進，所以離到期還差得遠、看門狗完全沒
# 觸發；而 run_weekly_sync.sh 的 START_TIME 用 `date +%s`。兩個時鐘不同 ⇒ 宣稱的
# 60 分上限是假的。

def _fake_date_dir(tmp_path, speedup):
    """造一個讓牆上時鐘「加速流動」的 `date` shim。

    真的讓機器睡覺沒辦法在測試裡做，但那件事的**可觀測後果**可以精準模擬：
    牆上時鐘大步前進，而 `sleep` 只走了幾秒。

    ⚠️ 第一版我用固定偏移（date 一律 +700 秒），結果**同時**套用在
    「算期限」與「比對期限」兩邊、剛好互相抵銷，測試照樣綠。所以 shim 必須記住
    第一次被呼叫的時間當基準（期限就是用那一刻算的），之後才讓時間加速——
    這樣「期限算完之後時鐘才跳」才真的被模擬到。
    """
    d = tmp_path / 'shim'
    d.mkdir()
    sh = d / 'date'
    sh.write_text(
        '#!/bin/bash\n'
        'if [ "$1" = "+%s" ]; then\n'
        '  now=$(/bin/date +%s)\n'
        f'  f="{d}/.t0"\n'
        '  [ -f "$f" ] || echo "$now" > "$f"\n'
        '  t0=$(cat "$f")\n'
        f'  echo $(( t0 + (now - t0) * {speedup} ))\n'
        'else\n'
        '  exec /bin/date "$@"\n'
        'fi\n', encoding='utf-8')
    sh.chmod(0o755)
    return str(d)


def test_watchdog_fires_when_wall_clock_jumps_past_the_deadline(tmp_path):
    """牆上時鐘跳過期限就要觸發，即使實際只過了幾秒（＝機器睡過一段時間）。

    MAX=600 秒，時鐘以 1000 倍速前進（期限算完之後才開始跳）。用 sleep 計時的
    版本要等 10 分鐘才會動；用牆上時鐘的版本必須在幾秒內判定超時。
    """
    shim = _fake_date_dir(tmp_path, 1000)   # 1 真實秒 = 1000 牆上秒
    e = dict(os.environ, LOG_DIR=str(tmp_path), PATH=shim + os.pathsep + os.environ['PATH'])
    e.update({'MAX_RUNTIME_SECONDS': '600', 'TIMEOUT_GRACE_SECONDS': '1',
              'WD_POLL_SECONDS': '1', 'WORK_SECONDS': '20'})
    t0 = time.monotonic()
    p = subprocess.run(['bash', HARNESS], env=e, capture_output=True, text=True, timeout=120)
    elapsed = time.monotonic() - t0
    events = (tmp_path / 'events').read_text(encoding='utf-8')

    assert 'msg=TIMEOUT' in events, f'牆上時鐘已跳過期限卻沒超時: {events}'
    assert p.returncode == 1
    assert elapsed < 60, f'用 sleep 計時就會等滿 600 秒；實際 {elapsed:.0f} 秒'


def test_watchdog_does_not_fire_before_the_wall_clock_deadline(tmp_path):
    """反面：時鐘沒跳過期限就不可誤殺（否則上面那條用「永遠超時」也會過）。"""
    shim = _fake_date_dir(tmp_path, 1)      # 不加速
    e = dict(os.environ, LOG_DIR=str(tmp_path), PATH=shim + os.pathsep + os.environ['PATH'])
    e.update({'MAX_RUNTIME_SECONDS': '600', 'TIMEOUT_GRACE_SECONDS': '1',
              'WD_POLL_SECONDS': '1', 'WORK_SECONDS': '2'})
    p = subprocess.run(['bash', HARNESS], env=e, capture_output=True, text=True, timeout=120)
    events = (tmp_path / 'events').read_text(encoding='utf-8')
    assert 'msg=TIMEOUT' not in events
    assert p.returncode == 0


def test_watchdog_never_sleeps_the_whole_budget_in_one_go():
    """守門：不得退回 `sleep "${MAX_RUNTIME_SECONDS}"`。

    那是 10/06 失效的根因——一次長 sleep 在系統睡眠期間完全不前進。
    掃描時去掉註解行，否則被上面那段說明文字騙過去。
    """
    import re
    with open(os.path.join(ROOT, 'lib', 'watchdog.sh'), encoding='utf-8') as f:
        lines = [l for l in f.read().split('\n') if not l.lstrip().startswith('#')]
    body = '\n'.join(lines)
    bad = re.findall(r'sleep\s+"?\$\{?MAX_RUNTIME_SECONDS', body)
    assert not bad, f'看門狗又用一次長 sleep 計時: {bad}'
    assert 'wd_wait_until' in body, 'wd_wait_until 不見了'


def test_watchdog_poll_nap_is_bounded():
    """單次 nap 必須被 WD_POLL_SECONDS 夾住，否則長 sleep 問題會從後門回來。"""
    with open(os.path.join(ROOT, 'lib', 'watchdog.sh'), encoding='utf-8') as f:
        src = f.read()
    assert 'nap="$WD_POLL_SECONDS"' in src
    assert '[ "$remain" -lt "$nap" ] && nap="$remain"' in src


# ---------- caffeinate：排程喚醒是 DarkWake，沒 assertion 會被凍結 ----------

def _sync_script():
    with open(os.path.join(ROOT, 'run_weekly_sync.sh'), encoding='utf-8') as f:
        return f.read()


def test_sync_script_reexecs_under_caffeinate():
    src = _sync_script()
    assert 'exec caffeinate -i' in src, (
        'run_weekly_sync.sh 沒有在 caffeinate 下重啟——pmset 的 DarkWake 維護窗'
        '（cap 180 秒）結束後系統會睡回去，process 被凍結在原地')


def test_caffeinate_wrapper_has_a_reentry_guard():
    """沒有旗標就會無限 re-exec 自己。"""
    src = _sync_script()
    assert 'WEEKLYSYNC_CAFFEINATED' in src
    assert 'export WEEKLYSYNC_CAFFEINATED=1' in src
    i = src.index('export WEEKLYSYNC_CAFFEINATED=1')
    j = src.index('exec caffeinate -i')
    assert i < j, '旗標必須在 exec 之前設好，否則 re-exec 後的那輪不知道自己已包過'


def test_caffeinate_wrapper_runs_before_start_time_and_marker():
    """re-exec 會換掉 $$，所以必須早於 TIMEOUT_MARKER（用 $$）與 START_TIME。"""
    src = _sync_script()
    assert src.index('exec caffeinate -i') < src.index('TIMEOUT_MARKER=')
    assert src.index('exec caffeinate -i') < src.index('START_TIME=')


def test_missing_caffeinate_is_reported_not_silent():
    """找不到 caffeinate 不中止同步，但不可無聲——這個缺口原本就是無聲的。"""
    src = _sync_script()
    k = src.index('exec caffeinate -i')
    tail = src[k:k + 500]
    assert '找不到 caffeinate' in tail


def test_caffeinate_actually_holds_the_assertion():
    """實證：caffeinate -i 真的建立 PreventUserIdleSystemSleep assertion。

    不是「我假設它有效」——直接問 pmset。macOS 以外跳過。
    """
    if subprocess.run(['uname'], capture_output=True, text=True).stdout.strip() != 'Darwin':
        pytest.skip('macOS only')
    out = subprocess.run(
        ['caffeinate', '-i', 'bash', '-c',
         'pmset -g assertions | grep "pid $$\\|caffeinate"'],
        capture_output=True, text=True, timeout=60).stdout
    assert 'PreventUserIdleSystemSleep' in out, out
    assert 'caffeinate' in out
