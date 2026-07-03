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
