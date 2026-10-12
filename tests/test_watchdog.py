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


def _fake_awake_dir(tmp_path, speedup):
    """造一個讓「醒著的時鐘」加速流動的 shim（給 WD_AWAKE_CMD 用）。

    真的讓機器睡覺沒辦法在測試裡做，但兩個時鐘**走不同速度**這件事可以精準模擬：
    牆上時鐘用 date shim 控制、清醒時鐘用這個控制，兩者分開才驗得出「誰觸發的」。
    """
    d = tmp_path / 'awake'
    d.mkdir()
    sh = d / 'awake_clock'
    sh.write_text(
        '#!/bin/bash\n'
        'now=$(/bin/date +%s)\n'
        f'f="{d}/.t0"\n'
        '[ -f "$f" ] || echo "$now" > "$f"\n'
        't0=$(cat "$f")\n'
        f'echo $(( (now - t0) * {speedup} ))\n', encoding='utf-8')
    sh.chmod(0o755)
    return str(sh)


def _run_env(tmp_path, *, wall_speedup=1, awake_speedup=1, **env):
    shim = _fake_date_dir(tmp_path, wall_speedup)
    e = dict(os.environ, LOG_DIR=str(tmp_path),
             PATH=shim + os.pathsep + os.environ['PATH'],
             WD_AWAKE_CMD=_fake_awake_dir(tmp_path, awake_speedup))
    e.update({k: str(v) for k, v in env.items()})
    return e


# ---------- 2026-10-12：兩個期限分開。睡過頭 ≠ 卡住 ----------
#
# 10/09 的誤殺：機器睡了 81.6 分鐘，醒來時牆上時鐘早就過了「開始後 60 分」，於是
# 看門狗在第一次醒來就把執行砍掉——而那一輪實際只跑了 0.4 分鐘。砍它符合當時的
# 政策，但那個政策把「睡過頭」當成「卡住」。
#
# 實測這台（10/12）：開機 240.94 小時、CLOCK_UPTIME_RAW 只有 54.13 小時。
# 電池供電不會插電，所以這不是邊角情境，是常態。

def test_wall_clock_jump_alone_does_not_kill_a_run_that_is_not_stuck(tmp_path):
    """牆上時鐘狂跳、清醒時鐘幾乎沒動 → **不可**殺。這就是 10/09 那一輪。"""
    e = _run_env(tmp_path, wall_speedup=1000, awake_speedup=1,
                 MAX_RUNTIME_SECONDS=600, MAX_WALL_SECONDS=0,
                 TIMEOUT_GRACE_SECONDS=1, WD_POLL_SECONDS=1, WORK_SECONDS=2)
    p = subprocess.run(['bash', HARNESS], env=e, capture_output=True, text=True, timeout=120)
    events = (tmp_path / 'events').read_text(encoding='utf-8')
    assert 'msg=TIMEOUT' not in events, (
        f'機器睡掉的時間被當成執行時間，又把沒卡住的一輪砍了: {events}')
    assert p.returncode == 0


def test_awake_budget_fires_when_the_run_is_really_stuck(tmp_path):
    """清醒時鐘超過預算 → 要殺，而且成因是 awake。

    牆上時鐘故意**不**加速：單獨驗清醒那一側，不讓兩個期限互相掩護。
    """
    e = _run_env(tmp_path, wall_speedup=1, awake_speedup=1000,
                 MAX_RUNTIME_SECONDS=600, MAX_WALL_SECONDS=0,
                 TIMEOUT_GRACE_SECONDS=1, WD_POLL_SECONDS=1, WORK_SECONDS=20)
    t0 = time.monotonic()
    p = subprocess.run(['bash', HARNESS], env=e, capture_output=True, text=True, timeout=120)
    elapsed = time.monotonic() - t0
    events = (tmp_path / 'events').read_text(encoding='utf-8')
    log = (tmp_path / 'harness.log').read_text(encoding='utf-8')

    assert 'msg=TIMEOUT:awake' in events, f'清醒預算用完卻沒觸發: {events}'
    assert p.returncode == 1
    assert '已達執行時間上限' in log and '清醒計時' in log
    assert elapsed < 60, f'用一次長 sleep 計時就會等滿 600 秒；實際 {elapsed:.0f} 秒'


def test_wall_deadline_fires_when_set_and_says_so(tmp_path):
    """牆上上限仍然存在（它守的是「不要壓到下一次排程」），成因是 wall。"""
    e = _run_env(tmp_path, wall_speedup=1000, awake_speedup=1,
                 MAX_RUNTIME_SECONDS=600, MAX_WALL_SECONDS=1200,
                 TIMEOUT_GRACE_SECONDS=1, WD_POLL_SECONDS=1, WORK_SECONDS=20)
    p = subprocess.run(['bash', HARNESS], env=e, capture_output=True, text=True, timeout=120)
    events = (tmp_path / 'events').read_text(encoding='utf-8')
    log = (tmp_path / 'harness.log').read_text(encoding='utf-8')

    assert 'msg=TIMEOUT:wall' in events, f'牆上上限沒觸發: {events}'
    assert p.returncode == 1
    assert '已達牆上時間上限' in log, '訊息要指明是哪一個期限，否則操作者查錯方向'


def test_neither_deadline_fires_on_a_normal_run(tmp_path):
    """反面對照：兩個時鐘都正常走時不可誤殺（否則上面兩條用「永遠超時」也會過）。"""
    e = _run_env(tmp_path, wall_speedup=1, awake_speedup=1,
                 MAX_RUNTIME_SECONDS=600, MAX_WALL_SECONDS=1200,
                 TIMEOUT_GRACE_SECONDS=1, WD_POLL_SECONDS=1, WORK_SECONDS=2)
    p = subprocess.run(['bash', HARNESS], env=e, capture_output=True, text=True, timeout=120)
    events = (tmp_path / 'events').read_text(encoding='utf-8')
    assert 'msg=TIMEOUT' not in events
    assert p.returncode == 0


def test_missing_awake_clock_degrades_loudly(tmp_path):
    """取不到清醒時鐘就退回牆上時鐘——行為等於改版前，但**不可無聲**。

    無聲降級的話，清醒預算會悄悄變成牆上預算，也就是 10/09 那個誤殺又回來了，
    而且沒有人會知道。
    """
    broken = tmp_path / 'broken_clock'
    broken.write_text('#!/bin/bash\nexit 1\n', encoding='utf-8')
    broken.chmod(0o755)
    e = dict(os.environ, LOG_DIR=str(tmp_path), WD_AWAKE_CMD=str(broken),
             WD_PYTHON='/nonexistent/python')
    e.update({'MAX_RUNTIME_SECONDS': '2', 'MAX_WALL_SECONDS': '0',
              'TIMEOUT_GRACE_SECONDS': '1', 'WD_POLL_SECONDS': '1',
              'WORK_SECONDS': '60'})
    p = subprocess.run(['bash', HARNESS], env=e, capture_output=True, text=True, timeout=120)
    log = (tmp_path / 'harness.log').read_text(encoding='utf-8')

    assert '取不到「醒著的時間」時鐘' in log + p.stderr, (
        '降級沒有出聲：清醒預算悄悄變回牆上預算')
    assert p.returncode == 1, '降級之後上限仍然要有效，不可變成完全沒有看門狗'


def test_missing_awake_clock_warns_exactly_once(tmp_path):
    """降級警告只能印一次，不是每個輪詢間隔印一次。

    `$(wd_awake_now)` 的 command substitution 會起子 shell，warn-once 旗標設在
    裡面就跟著消失——22 小時的牆上期限配 15 秒輪詢約 5,280 行，把日誌洗掉
    （#117 review P2）。所以這裡**精確數次數**，不是只檢查存在。

    要真的碰到：輪詢間隔 1 秒、工作 5 秒 ⇒ 至少 4 次取時鐘。壞掉的版本會寫
    4 行以上。

    ⚠️ 這條只對「**兩個**呼叫點都在 command substitution 裡」敏感。`wd_start`
    在 fork 之前那次直接呼叫，是旗標能被看門狗子 shell 繼承的關鍵——只還原迴圈
    裡那一個呼叫點的話，重複警告會被父 shell 的旗標壓住，注入就抓不到（實際用
    不完整的注入試過一次，誤以為測試無效）。
    """
    broken = tmp_path / 'broken_clock'
    broken.write_text('#!/bin/bash\nexit 1\n', encoding='utf-8')
    broken.chmod(0o755)
    e = dict(os.environ, LOG_DIR=str(tmp_path), WD_AWAKE_CMD=str(broken),
             WD_PYTHON='/nonexistent/python')
    e.update({'MAX_RUNTIME_SECONDS': '60', 'MAX_WALL_SECONDS': '0',
              'TIMEOUT_GRACE_SECONDS': '1', 'WD_POLL_SECONDS': '1',
              'WORK_SECONDS': '5'})
    p = subprocess.run(['bash', HARNESS], env=e, capture_output=True, text=True,
                       timeout=120)
    log = (tmp_path / 'harness.log').read_text(encoding='utf-8')
    warnings = log.count('取不到「醒著的時間」時鐘')

    assert warnings == 1, (
        f'降級警告印了 {warnings} 次（應該 1 次）。'
        'warn-once 的旗標若設在 command substitution 的子 shell 裡就留不住，'
        '長時間執行會把日誌洗掉')
    assert p.returncode == 0, f'這一輪不該超時（只是降級），實際 {p.returncode}'


def test_grace_uses_the_awake_clock(tmp_path):
    """寬限也要用清醒時鐘算。

    牆上計時的話，機器在 cleanup 期間睡著，SIGKILL 會在醒來那一刻立刻落下，
    通知根本沒機會寄出——和 10/09 同一個錯誤，只是換到寬限期。
    這裡：牆上時鐘加速 1000 倍、清醒時鐘正常，cleanup 睡 4 秒而寬限 8 秒
    （清醒），所以 cleanup 必須跑完。
    """
    # 寬限的牆上側只是最後保險（預設 60 倍）。這裡把它放大，否則 1000 倍速的
    # 牆上時鐘會在 0.5 秒內吃掉 60×8＝480 牆上秒，驗到的就不是清醒那一側了。
    e = _run_env(tmp_path, wall_speedup=1000, awake_speedup=1,
                 MAX_RUNTIME_SECONDS=1, MAX_WALL_SECONDS=0,
                 TIMEOUT_GRACE_SECONDS=8, WD_POLL_SECONDS=1,
                 WD_GRACE_WALL_FACTOR=1000,
                 WORK_SECONDS=60, CLEANUP_HANG_SECONDS=4)
    subprocess.run(['bash', HARNESS], env=e, capture_output=True, text=True, timeout=120)
    events = (tmp_path / 'events').read_text(encoding='utf-8')
    assert 'CLEANUP_DONE' in events, (
        f'寬限被牆上時鐘吃掉，cleanup 沒跑完就被 SIGKILL: {events}')


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


def test_grace_has_a_wall_clock_backstop(tmp_path):
    """寬限的牆上側是最後保險：睡著的機器不可把殭屍無限期留著。

    與上一條成對——上一條證明清醒時鐘在管，這一條證明牆上保險仍然會落下。
    cleanup 睡 30 秒，而牆上保險只有 2×1＝2 牆上秒（1000 倍速 ⇒ 立刻到期）。
    """
    e = _run_env(tmp_path, wall_speedup=1000, awake_speedup=1,
                 MAX_RUNTIME_SECONDS=1, MAX_WALL_SECONDS=0,
                 TIMEOUT_GRACE_SECONDS=1, WD_POLL_SECONDS=1,
                 WD_GRACE_WALL_FACTOR=2,
                 WORK_SECONDS=60, CLEANUP_HANG_SECONDS=30)
    p = subprocess.run(['bash', HARNESS], env=e, capture_output=True, text=True, timeout=120)
    events = (tmp_path / 'events').read_text(encoding='utf-8')
    assert 'CLEANUP_START' in events
    assert 'CLEANUP_DONE' not in events, 'cleanup 卡住卻沒被保險終止＝殭屍會留著'
    assert p.returncode != 0


# ---------- 正式腳本的兩個期限（harness 有自己的預設，守不到這裡） ----------
#
# 故障注入時這條漏了：把 run_weekly_sync.sh 的 MAX_WALL_SECONDS 整行拿掉，20 條
# 全綠。因為測試驅動的是 harness，而 harness 自己有 `MAX_WALL_SECONDS:-0`，
# 正式腳本的預設值等於沒人在守。同一個檔案已有同類前例
# （test_sync_script_sources_the_shared_watchdog、…_reexecs_under_caffeinate）。

def _sync_script_body():
    """去掉註解行的 run_weekly_sync.sh。

    不去掉的話，上面這段說明文字裡的 MAX_WALL_SECONDS 就會讓斷言自己過關
    （同類假綠在 #101/#104/#105 各踩過一次）。
    """
    return '\n'.join(l for l in _sync_script().split('\n')
                     if not l.lstrip().startswith('#'))


def test_sync_script_sets_a_sane_wall_deadline():
    import re as _re
    body = _sync_script_body()
    m = _re.search(r'MAX_WALL_SECONDS="\$\{MAX_WALL_SECONDS:-(\d+)\}"', body)
    assert m, 'run_weekly_sync.sh 沒設 MAX_WALL_SECONDS 預設值，壓到下一次排程的防線就只在測試裡存在'
    wall = int(m.group(1))
    runtime = _re.search(r'MAX_RUNTIME_SECONDS="\$\{MAX_RUNTIME_SECONDS:-(\d+)\}"', body)
    assert runtime, '清醒預算的預設值也不見了'
    assert wall > int(runtime.group(1)), (
        f'牆上上限 {wall}s 不大於清醒預算 {runtime.group(1)}s，'
        '那清醒預算永遠用不完就先被牆上那側砍掉——等於這個 PR 沒改到東西')
    assert wall < 86400, (
        f'牆上上限 {wall}s 超過一天，就壓到下一次排程了（launchd 每天 10:00）')


def test_sync_script_distinguishes_the_two_timeout_reasons():
    """兩種成因的告警文字要分開：一種去查 Drive 連線，一種去查電源與喚醒排程。"""
    body = _sync_script_body()
    assert 'TIMEOUT_BY=' in body, '沒讀 marker 內容，成因在這一步就丟掉了'
    assert '*wall*' in body, '沒有針對牆上超時的分支'
    assert body.count('ERROR_MESSAGE=') >= 3, (
        '超時應該有兩種訊息（awake／wall）加上原本的未預期錯誤，'
        f'只找到 {body.count("ERROR_MESSAGE=")} 個')
