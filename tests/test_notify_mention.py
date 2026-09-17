"""notify：@mention 用結構化 tag block、失敗通知走 ClickUp（批次 A1/A3）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import geobingan_sync.notify as notify


def test_payload_plain_without_mention():
    assert notify.build_clickup_comment_payload('hi') == {'comment_text': 'hi'}


def test_payload_tag_block_with_mention():
    p = notify.build_clickup_comment_payload('hi', mention_user_id='48123565')
    assert p['comment'][0] == {'type': 'tag', 'user': {'id': 48123565}}
    assert p['comment'][1]['text'].endswith('hi')
    assert p['notify_all'] is False


def _capture(monkeypatch):
    """攔下**所有**對外通道，回傳 (ClickUp 紀錄, Email 紀錄)。

    原本這裡只擋 ClickUp／macOS／LINE 三條。PR #83 把 Email 加進 error 級通知後，
    `send_failure()` 就會真的走 Gmail SMTP 寄出——而本檔斷言只看 ClickUp，所以
    測試全綠，實際上每跑一次全套就寄一封「❌ geoBingAn 同步失敗／磁碟滿」到
    zhe@geothings.tw（2026-09-17 一天洗了十幾封假警報）。
    共用程式碼新增通道時，既有測試不會自己知道要 stub，所以另有 conftest.py
    的 autouse 防線擋在 smtplib 層；這裡則把 Email 納入斷言，讓行為被寫明。
    """
    sent, emails = [], []
    monkeypatch.setattr(notify, 'CLICKUP_TOKEN', 'tok')
    monkeypatch.setattr(notify, 'HEALTHCHECK_CLICKUP_TASK_ID', 'task1')
    monkeypatch.setattr(notify, 'ALERT_MENTION_USER_ID', '48123565')
    monkeypatch.setattr(notify, 'ENABLE_MACOS_NOTIFY', False)
    monkeypatch.setattr(notify, 'LINE_NOTIFY_TOKEN', '')
    monkeypatch.setattr(notify, 'send_clickup_comment',
                        lambda task_id, message, mention_user_id=None: sent.append((task_id, message, mention_user_id)) or True)
    monkeypatch.setattr(notify, 'send_email_alert',
                        lambda subject, body: emails.append((subject, body)) or True)
    return sent, emails


def test_send_failure_routes_to_clickup_with_mention(monkeypatch):
    sent, emails = _capture(monkeypatch)
    notify.send_failure('執行失敗', '磁碟滿')
    assert sent and sent[0][0] == 'task1' and sent[0][2] == '48123565'
    assert '磁碟滿' in sent[0][1]


def test_send_failure_also_sends_email(monkeypatch):
    """失敗是 error 級，Email 是唯一會推播到人的通道，必須真的走到（但用假的）。"""
    sent, emails = _capture(monkeypatch)
    notify.send_failure('執行失敗', '磁碟滿')
    assert len(emails) == 1
    subject, body = emails[0]
    assert '同步失敗' in subject and '磁碟滿' in body


def test_send_warning_routes_to_clickup_without_mention(monkeypatch):
    sent, _ = _capture(monkeypatch)
    notify.send_warning('警告', 'x')
    assert sent and sent[0][2] is None


def test_send_notification_mention_flag(monkeypatch):
    sent, _ = _capture(monkeypatch)
    notify.send_notification('t', 'm', use_clickup=True, mention=False)
    notify.send_notification('t', 'm', use_clickup=True, mention=True)
    assert [s[2] for s in sent] == [None, '48123565']
