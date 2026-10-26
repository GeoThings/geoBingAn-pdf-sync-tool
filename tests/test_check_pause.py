"""Tests for health_check.check_pause — stale-pause guard (#57-class silent-lock guard)."""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import health_check


class TestCheckPause:
    def test_absent_ok(self, monkeypatch):
        monkeypatch.setattr(health_check.os.path, 'exists', lambda p: False)
        status, msg = health_check.check_pause()
        assert status == 'ok'
        assert '未暫停' in msg

    def test_recent_pause_warning(self, monkeypatch):
        monkeypatch.setattr(health_check.os.path, 'exists', lambda p: True)
        monkeypatch.setattr(health_check.os.path, 'getmtime', lambda p: time.time() - 3 * 86400)
        status, msg = health_check.check_pause()
        assert status == 'warning'          # 存在但未逾窗 → 提醒
        assert '3.0 天' in msg

    def test_stale_pause_escalates_to_error(self, monkeypatch):
        # 逾 30 天檔名窗 → error + 帶補掃指引（避免恢復時漏報告）
        monkeypatch.setattr(health_check.os.path, 'exists', lambda p: True)
        monkeypatch.setattr(health_check.os.path, 'getmtime', lambda p: time.time() - 40 * 86400)
        status, msg = health_check.check_pause()
        assert status == 'error'
        assert '--catchup-days' in msg

    def test_threshold_boundary_30_days(self, monkeypatch):
        # 剛好 30 天 = 已進入風險區 → error
        monkeypatch.setattr(health_check.os.path, 'exists', lambda p: True)
        monkeypatch.setattr(health_check.os.path, 'getmtime', lambda p: time.time() - 30 * 86400 - 60)
        status, _ = health_check.check_pause()
        assert status == 'error'


class TestTheThresholdIsNotACopy:
    """門檻必須跟著 upload_pdfs 的檔名日期窗走，不可以是自己抄來的一份。

    `check_pause` 存在的唯一目的就是「暫停超過那個窗就警告」——因為恢復時
    落在窗外的報告會被**靜默漏掉**（去重靠上傳歷史，不會自己補回來）。
    如果它比對的是自己抄的 30，那麼有人改了 `upload_pdfs` 的窗之後，這個警告
    就會在錯的時間點亮或不亮，而它是唯一會提醒「恢復時要補掃」的地方。

    2026-10-27 查到這個數字被硬寫在兩個檔案共四處，而真正生效的只有
    `upload_pdfs.main()` 裡那個字面常數；`.pause_upload` 的註記甚至寫成 21 天
    （那是 `DAYS_AGO`，跟補掃門檻是兩個不同的窗）。
    """

    def _pause_for(self, monkeypatch, days):
        monkeypatch.setattr(health_check.os.path, 'exists', lambda p: True)
        monkeypatch.setattr(health_check.os.path, 'getmtime',
                            lambda p: time.time() - days * 86400)

    def test_the_threshold_follows_the_upload_cutoff(self, monkeypatch):
        from geobingan_sync.steps import upload_pdfs
        monkeypatch.setattr(upload_pdfs, 'FILENAME_DATE_CUTOFF_DAYS', 10)

        self._pause_for(monkeypatch, 12)
        assert health_check.check_pause()[0] == 'error', \
            '窗改成 10 天、已暫停 12 天，門檻卻沒跟著動——那是一份拷貝'

        self._pause_for(monkeypatch, 8)
        assert health_check.check_pause()[0] == 'warning', \
            '窗改成 10 天、只暫停 8 天，卻已經 error——門檻沒跟著動'

    def test_the_message_names_the_real_window(self, monkeypatch):
        """訊息裡的天數也要是真的那個值，不可以是寫死的文字。"""
        from geobingan_sync.steps import upload_pdfs
        monkeypatch.setattr(upload_pdfs, 'FILENAME_DATE_CUTOFF_DAYS', 10)
        self._pause_for(monkeypatch, 12)
        _, msg = health_check.check_pause()
        assert '10' in msg, f'訊息沒有講出真正的窗：{msg}'
        assert '30' not in msg, f'訊息還在講寫死的 30 天：{msg}'

    def test_the_constant_is_what_upload_actually_uses(self):
        """常數要真的被 upload_pdfs 用到，不是擺著好看。"""
        import ast, inspect
        from geobingan_sync.steps import upload_pdfs
        assert isinstance(upload_pdfs.FILENAME_DATE_CUTOFF_DAYS, int)
        tree = ast.parse(inspect.getsource(upload_pdfs.main))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert 'FILENAME_DATE_CUTOFF_DAYS' in names, \
            'upload_pdfs.main() 沒有用那個常數——還是用字面值的話，常數就只是裝飾'
