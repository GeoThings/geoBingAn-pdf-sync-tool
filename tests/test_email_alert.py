"""Email 告警通道：組信、送達判準、error/warning 分級（2026-09-16 實測後新增）。

背景：ClickUp 通道對 token 擁有者本人無效——機器人用他的帳號發文，ClickUp 會
吃掉自我 @、也不通知自己發的留言。Email 是實測唯一會推播到手機的通道。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import geobingan_sync.notify as notify
import health_check


def test_build_email_encodes_chinese_subject():
    raw = notify.build_email('❌ 解析積壓', '116 份卡住', 'a@b.c', 'd@e.f')
    assert 'From: a@b.c' in raw and 'To: d@e.f' in raw
    assert 'Subject: ' in raw and '解析積壓' not in raw.split('\n')[0]   # 中文主旨經 MIME 編碼


def test_send_email_alert_returns_false_when_unconfigured(monkeypatch):
    monkeypatch.setattr(notify, 'ALERT_EMAIL_TO', '')
    monkeypatch.setattr(notify, 'ALERT_SMTP_PASSWORD', '')
    assert notify.send_email_alert('t', 'b') is False


def _configure(monkeypatch, sent, fail=False):
    monkeypatch.setattr(notify, 'ALERT_EMAIL_TO', 'zhe@example.com')
    monkeypatch.setattr(notify, 'ALERT_EMAIL_FROM', '')
    monkeypatch.setattr(notify, 'ALERT_SMTP_PASSWORD', 'app-password')
    monkeypatch.setattr(notify, 'ENABLE_MACOS_NOTIFY', False)
    monkeypatch.setattr(notify, 'LINE_NOTIFY_TOKEN', '')

    class _SMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, u, p): sent.append(('login', u))
        def sendmail(self, frm, to, raw):
            if fail:
                raise OSError('smtp down')
            sent.append(('mail', to[0], raw))
    import smtplib
    monkeypatch.setattr(smtplib, 'SMTP_SSL', _SMTP)


def test_send_email_alert_success(monkeypatch):
    sent = []
    _configure(monkeypatch, sent)
    assert notify.send_email_alert('❌ 告警', '內容') is True
    assert sent[0] == ('login', 'zhe@example.com')
    assert sent[1][1] == 'zhe@example.com'


def test_send_email_alert_failure_returns_false(monkeypatch):
    sent = []
    _configure(monkeypatch, sent, fail=True)
    assert notify.send_email_alert('t', 'b') is False


def test_send_notification_email_only_when_requested(monkeypatch):
    sent = []
    _configure(monkeypatch, sent)
    monkeypatch.setattr(notify, 'CLICKUP_TOKEN', '')
    notify.send_notification('t', 'm', use_email=False)
    assert sent == []
    results = notify.send_notification('t', 'm', use_email=True)
    assert ('Email', True) in results


def test_error_delivery_judged_by_email_not_clickup(monkeypatch):
    """error 級：ClickUp 成功但 Email 失敗 → 不算送達（下輪會重試）。"""
    monkeypatch.setattr('geobingan_sync.config.ALERT_EMAIL_TO', 'zhe@example.com')
    monkeypatch.setattr('geobingan_sync.config.ALERT_SMTP_PASSWORD', 'pw')
    monkeypatch.setattr(notify, 'send_notification',
                        lambda *a, **k: [('ClickUp', True), ('Email', False)])
    assert health_check.clickup_send('t', 'b', True) is False
    monkeypatch.setattr(notify, 'send_notification',
                        lambda *a, **k: [('ClickUp', False), ('Email', True)])
    assert health_check.clickup_send('t', 'b', True) is True


def test_warning_delivery_still_judged_by_clickup(monkeypatch):
    """warning 級純紀錄，不寄信、ClickUp 成功即可。"""
    monkeypatch.setattr('geobingan_sync.config.ALERT_EMAIL_TO', 'zhe@example.com')
    monkeypatch.setattr('geobingan_sync.config.ALERT_SMTP_PASSWORD', 'pw')
    captured = {}
    def fake(title, body, **kw):
        captured.update(kw)
        return [('ClickUp', True)]
    monkeypatch.setattr(notify, 'send_notification', fake)
    assert health_check.clickup_send('t', 'b', False) is True
    assert captured.get('use_email') is False        # warning 不寄信


def test_falls_back_to_clickup_when_email_unconfigured(monkeypatch):
    """沒設 Email 時不可整條卡死：退回看 ClickUp。"""
    monkeypatch.setattr('geobingan_sync.config.ALERT_EMAIL_TO', '')
    monkeypatch.setattr('geobingan_sync.config.ALERT_SMTP_PASSWORD', '')
    monkeypatch.setattr(notify, 'send_notification', lambda *a, **k: [('ClickUp', True)])
    assert health_check.clickup_send('t', 'b', True) is True
