"""測試期間一律禁止真的寄信。

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
import smtplib

import pytest


class RealEmailBlocked(RuntimeError):
    pass


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
