"""測試期間一律禁止碰到外部通道與正式 state 檔。

2026-09-17 事故：`tests/test_notify_mention.py` 的 `_capture()` 只擋了 ClickUp、
macOS 與 LINE 三條通道。PR #83 後來把 Email 加進失敗通知（error 級一律寄信），
這支既有測試就開始**真的寄信到 zhe@geothings.tw**，而它的斷言只看 ClickUp，
所以全綠、沒人發現。當天跑了十幾次全套測試 → 收件匣被十幾封「❌ geoBingAn
同步失敗／磁碟滿」洗版，而且那是假訊息（磁碟其實有 15GB）。

比逐一補 stub 更重要的是**擋在邊界**：共用程式碼日後再加第四條通道時，舊測試
還是不會知道要 stub。所以這裡直接把 smtplib 的連線建構子換掉，任何測試只要真
的想開 SMTP 連線就立刻失敗並指出該 stub 什麼。要驗寄信邏輯的測試（test_email_alert）
本來就自己注入假 SMTP，不受影響。
"""
import builtins
import io
import os
import smtplib
from pathlib import Path

import pytest


class RealEmailBlocked(RuntimeError):
    pass


class RealStateWriteBlocked(RuntimeError):
    pass


# ───────────────────────── 正式 state 目錄的寫入防線 ─────────────────────────
#
# 🔴 2026-10-12 實際踩到（PR #114 合併後在正式目錄跑整套才發現）：
# `tests/test_report_date_upload.py` 呼叫 `upload_pdfs.process_single_pdf()`，
# 它在成功路徑**立刻** `save_state(state)`，而 `STATE_FILE` 是相對 CWD 的
# `./state/uploaded_to_geobingan_7days.json`——從正式目錄跑 pytest 時那就是
# **營運中的 7 天上傳帳本**。`save_state` 是 read-merge-write（uploaded_files
# 取聯集），所以測試那筆假的「資料夾/檔名」會被永久併進帳本，那份 PDF 之後
# 永遠不會被上傳，而且沒有任何錯誤訊息——靜默、不可逆。
#
# 當天沒出事純屬運氣：測試用的檔名剛好是真的上傳過的那一份（帳本裡已有，
# 聯集後集合不變，只有順序變了）。同一支測試其他參數的檔名
# （`恆合-錦西街 日報表 115.09.16.pdf`）就不在帳本裡，換成那個就會真的少上傳。
#
# 這裡**不逐一列舉模組常數**：全套有 28 個指向 `./state/...` 的路徑分佈在 14 個
# 模組，列舉法漏掉下一個新增的常數時同樣是靜默的（conftest 開頭那條教訓）。
# 真正的邊界是**檔案路徑**，所以擋在開檔與 rename 這兩個收口上。


def _resolve(path):
    try:
        return Path(os.fsdecode(path)).resolve()
    except (TypeError, ValueError, OSError):
        return None


def _protected_state_dirs():
    """要保護的正式 state 目錄。**在 conftest 匯入時算好一次**。

    之後任何測試 chdir 都不會改變保護範圍——保護範圍若跟著 CWD 漂移，
    就會在最需要它的那一刻（有人 chdir 到暫存目錄）失效。

    兩個來源都要算：
    - `./state`：production 的路徑常數大多相對 CWD，從正式目錄跑整套時就是營運檔。
    - `REPO_ROOT/state`：`analyze_decline` 等模組用的是套件自己的位置。
    """
    from geobingan_sync import REPO_ROOT
    dirs = []
    for cand in (Path('state'), Path(REPO_ROOT) / 'state'):
        r = _resolve(cand)
        if r is not None and r not in dirs:
            dirs.append(r)
    return dirs


PROTECTED_STATE_DIRS = _protected_state_dirs()

#: conftest 匯入時就留住的真正檔案 API。防線自己要用（不能呼叫被自己換掉的版本），
#: 刻意觸發防線的守門測試清理殘留時也要用——它的 cleanup 若走被守住的 os.unlink，
#: 會在 teardown 再踩一次防線，把那支測試變成錯誤（實際踩過）。
_REAL_OPEN = builtins.open
_REAL_IO_OPEN = io.open
_REAL_REPLACE = os.replace
_REAL_RENAME = os.rename
_REAL_REMOVE = os.remove
_REAL_UNLINK = os.unlink

_WRITE_MODE_CHARS = ('w', 'a', 'x', '+')


def unguarded_unlink(path) -> None:
    """繞過防線刪一個檔。只給守門測試清理自己製造的殘留用。"""
    try:
        _REAL_UNLINK(path)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        pass


def is_protected_path(path) -> bool:
    """這個路徑落在正式 state 目錄裡嗎？（檔案描述子一律回 False）"""
    if isinstance(path, int):
        return False
    target = _resolve(path)
    if target is None:
        return False
    return any(target == d or d in target.parents for d in PROTECTED_STATE_DIRS)


def state_snapshot(dirs=None):
    """正式 state 目錄的（路徑 → mtime_ns、大小）快照。

    值刻意是 tuple 且**不經序列化**：我自己量這件事時先踩過一次，把快照
    `json.dump` 再讀回來，tuple 變成 list，於是每個檔案都比對成「被改過」，
    一度誤判成 10 個檔案外洩（真相是 2 個）。比對兩邊必須是同一種型別。
    """
    snap = {}
    for d in (dirs if dirs is not None else PROTECTED_STATE_DIRS):
        if not d.is_dir():
            continue
        for f in sorted(d.rglob('*')):
            if f.is_file():
                st = f.stat()
                snap[str(f)] = (st.st_mtime_ns, st.st_size)
    return snap


def snapshot_diff(before: dict, after: dict):
    """被新建、改寫或刪掉的檔案路徑（排序後）。"""
    return sorted(k for k in set(before) | set(after)
                  if before.get(k) != after.get(k))


@pytest.fixture(autouse=True)
def _no_real_state_writes(monkeypatch):
    """任何測試要寫進正式 state 目錄就當場失敗，並指出是哪個檔。

    擋兩個收口：開檔（含 `io.open`，`Path.write_text`／`Path.open` 走的是它）
    與 `os.replace`／`os.rename`（atomic 寫入的最後一步繞過開檔檢查——
    `upload_pdfs.save_state` 正是寫 `.tmp.<pid>` 再 replace）。

    讀取不擋：讀到正式檔是另一類問題（測試結果取決於今天跑成什麼樣），
    已經由下面那幾個 per-file fixture 各自導向暫存路徑。

    回傳 attempts list：**刻意要觸發防線**的守門測試拿到它之後自己 clear()，
    否則 teardown 的斷言會把那支測試判成違規（它正是來驗防線有效的）。
    """
    attempts = []

    def _blocked(path, what):
        attempts.append(str(path))
        raise RealStateWriteBlocked(
            f'測試想{what}正式 state 檔：{path}\n'
            '   這會改到營運中的資料（上傳帳本被併進假紀錄＝那份 PDF 永遠不會上傳）。\n'
            '   請把該模組的路徑常數 monkeypatch 到 tmp_path，或用現成的 fixture。')

    def guarded_open(file, mode='r', *args, **kwargs):
        if any(c in mode for c in _WRITE_MODE_CHARS) and is_protected_path(file):
            _blocked(file, '寫入')
        return _REAL_OPEN(file, mode, *args, **kwargs)

    def guarded_io_open(file, mode='r', *args, **kwargs):
        if any(c in mode for c in _WRITE_MODE_CHARS) and is_protected_path(file):
            _blocked(file, '寫入')
        return _REAL_IO_OPEN(file, mode, *args, **kwargs)

    def _guard_dst(fn, what):
        def wrapper(src, dst, *args, **kwargs):
            if is_protected_path(dst):
                _blocked(dst, what)
            return fn(src, dst, *args, **kwargs)
        return wrapper

    def _guard_target(fn, what):
        def wrapper(path, *args, **kwargs):
            if is_protected_path(path):
                _blocked(path, what)
            return fn(path, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(builtins, 'open', guarded_open)
    monkeypatch.setattr(io, 'open', guarded_io_open)
    monkeypatch.setattr(os, 'replace', _guard_dst(_REAL_REPLACE, '覆蓋'))
    monkeypatch.setattr(os, 'rename', _guard_dst(_REAL_RENAME, '覆蓋'))
    monkeypatch.setattr(os, 'remove', _guard_target(_REAL_REMOVE, '刪除'))
    monkeypatch.setattr(os, 'unlink', _guard_target(_REAL_UNLINK, '刪除'))
    yield attempts
    assert not attempts, (
        f'這支測試想寫正式 state 檔（{len(attempts)} 次）：{sorted(set(attempts))}')


@pytest.fixture(autouse=True, scope='session')
def _production_state_unchanged():
    """整套跑完後，正式 state 目錄必須與開跑前一模一樣。

    上面的防線擋的是「經過 Python 檔案 API 的寫入」；這一層是事後對帳，
    連 subprocess、`os.utime` 這類繞過去的路徑也抓得到。擋下來還不夠——
    10/12 那次就是因為沒人在對帳，整套全綠而帳本已經被改寫。

    偽陽性只有一種可能：測試期間排程的同步真的在寫 state。訊息裡要講清楚，
    不然下一個看到紅字的人會以為是測試壞了。
    """
    before = state_snapshot()
    yield
    changed = snapshot_diff(before, state_snapshot())
    assert not changed, (
        '整套測試期間正式 state 檔被改動了：\n   ' + '\n   '.join(changed) +
        '\n   （若此刻排程同步正在跑，那是偽陽性；否則就是有測試繞過防線寫進去了）')


@pytest.fixture(autouse=True)
def _no_real_upload_state(monkeypatch, tmp_path_factory):
    """上傳帳本、永久歷史與 PDF inventory 一律導向暫存路徑。

    有了上面的寫入防線，漏接的測試會**失敗**而不是污染資料；但失敗的測試仍要
    有路可走，所以這裡把 `upload_pdfs` 的四個路徑常數都指到暫存目錄。

    `STATE_LOCK_FILE` 一定要跟著換：它在 import 時就由 `STATE_FILE` 算好，
    只換 `STATE_FILE` 的話 flock 仍然開在正式目錄（實測正式帳本的 `.lock`
    mtime 真的被動到了）。這就是「cache key 要涵蓋所有 baked-in state」的同一
    個形狀：衍生常數不會跟著來源變。
    """
    from geobingan_sync.steps import upload_pdfs
    d = tmp_path_factory.mktemp('noupload')
    monkeypatch.setattr(upload_pdfs, 'STATE_FILE',
                        str(d / 'uploaded_to_geobingan_7days.json'))
    monkeypatch.setattr(upload_pdfs, 'STATE_LOCK_FILE',
                        str(d / 'uploaded_to_geobingan_7days.json.lock'))
    monkeypatch.setattr(upload_pdfs, 'HISTORY_FILE', str(d / 'upload_history_all.json'))
    monkeypatch.setattr(upload_pdfs, 'PDF_INVENTORY_FILE', str(d / 'pdf_inventory.json'))


@pytest.fixture(autouse=True)
def _no_real_email(monkeypatch, request):
    """autouse：每個測試都套用，不需要各檔自己記得。

    光擋下來還不夠——`notify.send_email_alert()` 把所有例外吞掉並回 False，
    所以擋掉之後測試仍然全綠，下一支犯同樣錯的測試依舊不會被發現。因此把嘗試
    記下來，在測試結束時讓它失敗，錯誤訊息直接指出是哪一支測試在寄信。
    """
    attempts = []

    def _blocked(*args, **kwargs):
        attempts.append(args[:1])
        raise RealEmailBlocked('測試嘗試建立真實 SMTP 連線')

    monkeypatch.setattr(smtplib, 'SMTP_SSL', _blocked)
    monkeypatch.setattr(smtplib, 'SMTP', _blocked)
    yield
    assert not attempts, (
        f'{request.node.nodeid} 嘗試真的寄出告警信（{len(attempts)} 次）。\n'
        '   注意 send_email_alert() 會吞掉例外回 False，所以不擋就會真的寄到收件匣。\n'
        '   請 monkeypatch notify.send_email_alert，或注入假的 SMTP client。')


@pytest.fixture(autouse=True)
def _no_real_pause_flag(monkeypatch, tmp_path_factory):
    """測試一律不得讀到正式目錄的 `.pause_upload`。

    2026-09-23 實際踩到：PR #96 讓 drain_stuck 讀 `./.pause_upload`，而當天營運上
    真的建了那個檔（本月 OpenAI 額度用完）。沒傳 pause_file 的 12 支測試因此全部
    回 EXIT_PAUSED，本機紅、CI 綠——**測試結果變成取決於操作者有沒有暫停上傳**。

    與 [[不可碰正式狀態]] 同一類問題（上一次是測試寄出真告警信、再上一次是測試
    污染預算帳本）。同樣擋在邊界：把預設路徑指向一個不存在的暫存位置，
    真正要驗暫停行為的測試自己傳 pause_file，不受影響。
    """
    from geobingan_sync.steps import drain_stuck
    fake = tmp_path_factory.mktemp('nopause') / '.pause_upload'   # 刻意不建立
    monkeypatch.setattr(drain_stuck, 'PAUSE_FILE', str(fake))


@pytest.fixture(autouse=True)
def _no_real_unsupported_file(monkeypatch, tmp_path_factory):
    """測試一律不得寫到正式的 `state/unsupported_sources.json`。

    同 `_no_real_pause_flag` 的理由，擋在邊界而不是要每支測試自己記得傳路徑：
    `_resolve_or_skip_indirect` 預設會寫這個檔，既有測試呼叫它時不會知道要隔離，
    一寫下去就把營運中的名單（含 first_seen）換成測試資料。
    """
    from geobingan_sync import unsupported_sources
    fake = tmp_path_factory.mktemp('unsupported') / 'unsupported_sources.json'
    monkeypatch.setattr(unsupported_sources, 'UNSUPPORTED_FILE', str(fake))


@pytest.fixture(autouse=True)
def _no_real_sync_status(monkeypatch, tmp_path_factory):
    """測試一律不得讀到正式的 `state/sync_status.json`。

    `check_unsupported_sources` 會拿最近一次同步時間來判斷「同步跑過卻沒名單」。
    若讀到正式檔，檢查結果就取決於本機今天有沒有跑過同步——在 worktree 綠、在
    正式目錄紅，或反過來。同 `.pause_upload` 的教訓：擋在邊界，要驗這段行為的
    測試自己傳 sync_status_path。
    """
    import health_check
    fake = tmp_path_factory.mktemp('nosync') / 'sync_status.json'   # 刻意不建立
    monkeypatch.setattr(health_check, 'SYNC_STATUS_FILE', str(fake))


@pytest.fixture(autouse=True)
def _no_real_sync_progress(monkeypatch, tmp_path_factory):
    """測試一律不得讀到正式的 `state/sync_permits_progress.json`。

    `check_sync_errors` 讀它判斷最近一輪有多少案出錯。正式檔在本機是活的
    （10/05 實測 2,067 筆錯誤、479 個 processed），讀到它測試結果就取決於今天
    同步跑成什麼樣。要驗這段行為的測試自己傳 path。
    """
    import health_check
    fake = tmp_path_factory.mktemp('noprog') / 'sync_permits_progress.json'
    monkeypatch.setattr(health_check, 'SYNC_PROGRESS_FILE', str(fake))


@pytest.fixture(autouse=True)
def _no_real_sync_permits_state(monkeypatch, tmp_path_factory):
    """測試一律不得讀寫正式的 `state/sync_permits_progress.json`。

    `PermitSync.__init__` 會 load_state()、錯誤路徑會 save_state()，而 STATE_FILE
    是相對 CWD 的 './state/...'。多支既有測試都會建 PermitSync 實例，從 repo 根
    執行時那就是**正式檔**——讀到活資料會讓結果取決於今天同步跑成什麼樣，寫下去
    更是直接污染正式狀態（同 feedback_tests_must_not_reach_outside：擋在邊界）。
    """
    from geobingan_sync.steps import sync_permits
    fake = tmp_path_factory.mktemp('nostate') / 'sync_permits_progress.json'
    monkeypatch.setattr(sync_permits, 'STATE_FILE', str(fake))


@pytest.fixture(autouse=True)
def _no_real_step_results(monkeypatch, tmp_path_factory):
    """測試一律不得寫到正式的 `state/step_result_*.json`。

    🔴 2026-10-06 查出這條真的在流血：`test_parse_pdf_list_urls` 會呼叫
    `PermitSync.run()`，它內部的 `_write_step_result()` 呼叫
    `step_results.accumulate()` **沒帶 base**，於是落到相對 CWD 的 './state'
    ——從 repo 根跑 pytest 就是**正式檔**。10/05 的真實數字 synced=194 就是這樣
    被測試覆蓋成 0 的。

    我前一天還明確排除過這個可能：只 grep 了測試裡對 `step_results.*` 的直接
    呼叫、看到都帶 `base=` 就下結論。漏掉的是「測試呼叫 production 程式碼、
    由它去寫」這條路徑——所以防線要擋在**模組的預設路徑**上，不是靠逐一檢查
    呼叫點（同 feedback_enforce_invariant_at_boundary）。
    """
    from geobingan_sync import step_results
    monkeypatch.setattr(step_results, '_BASE', str(tmp_path_factory.mktemp('nosteps')))
