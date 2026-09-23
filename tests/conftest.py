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
