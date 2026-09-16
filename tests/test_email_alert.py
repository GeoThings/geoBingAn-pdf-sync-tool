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


def test_error_recovery_uses_email_and_retries_on_failure(tmp_path, monkeypatch):
    """review P2：error 恢復也必須走 Email；Email 失敗不 commit，下一輪重試。"""
    import health_check
    from geobingan_sync.alert_state import AlertState

    monkeypatch.setattr('geobingan_sync.config.ALERT_EMAIL_TO', 'zhe@example.com')
    monkeypatch.setattr('geobingan_sync.config.ALERT_SMTP_PASSWORD', 'pw')
    calls = []

    def fake_send_notification(title, body, **kw):
        calls.append(kw.get('use_email'))
        return [('ClickUp', True), ('Email', fake_send_notification.email_ok)]
    fake_send_notification.email_ok = True
    monkeypatch.setattr(notify, 'send_notification', fake_send_notification)

    st = AlertState(tmp_path / 's.json')
    err = [('JWT Token', lambda: ('error', '已過期'))]
    ok_ = [('JWT Token', lambda: ('ok', '剩 7 天'))]

    health_check.run_health_check(checks=err, notify=True, alert_state=st)
    assert calls[-1] is True                       # 新告警走 Email

    # 恢復但 Email 失敗 → 不 commit
    fake_send_notification.email_ok = False
    health_check.run_health_check(checks=ok_, notify=True, alert_state=st)
    assert calls[-1] is True                       # 恢復同樣走 Email（不是只貼 ClickUp）
    assert st.load().get('JWT Token'), '未送達，狀態應保留以便重試'

    # 下一輪 Email 成功 → 才 commit
    fake_send_notification.email_ok = True
    health_check.run_health_check(checks=ok_, notify=True, alert_state=st)
    assert st.load() == {}, '送達後才清除'
