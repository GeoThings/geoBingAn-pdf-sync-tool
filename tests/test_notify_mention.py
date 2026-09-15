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
    sent = []
    monkeypatch.setattr(notify, 'CLICKUP_TOKEN', 'tok')
    monkeypatch.setattr(notify, 'HEALTHCHECK_CLICKUP_TASK_ID', 'task1')
    monkeypatch.setattr(notify, 'ALERT_MENTION_USER_ID', '48123565')
    monkeypatch.setattr(notify, 'ENABLE_MACOS_NOTIFY', False)
    monkeypatch.setattr(notify, 'LINE_NOTIFY_TOKEN', '')
    monkeypatch.setattr(notify, 'send_clickup_comment',
                        lambda task_id, message, mention_user_id=None: sent.append((task_id, message, mention_user_id)) or True)
    return sent


def test_send_failure_routes_to_clickup_with_mention(monkeypatch):
    sent = _capture(monkeypatch)
    notify.send_failure('執行失敗', '磁碟滿')
    assert sent and sent[0][0] == 'task1' and sent[0][2] == '48123565'
    assert '磁碟滿' in sent[0][1]


def test_send_warning_routes_to_clickup_without_mention(monkeypatch):
    sent = _capture(monkeypatch)
    notify.send_warning('警告', 'x')
    assert sent and sent[0][2] is None


def test_send_notification_mention_flag(monkeypatch):
    sent = _capture(monkeypatch)
    notify.send_notification('t', 'm', use_clickup=True, mention=False)
    notify.send_notification('t', 'm', use_clickup=True, mention=True)
    assert [s[2] for s in sent] == [None, '48123565']
