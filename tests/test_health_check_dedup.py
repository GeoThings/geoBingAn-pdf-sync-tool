"""health_check.run_health_check：一輪一則、重複抑制、error 才 @（批次 A 整合）。"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import health_check
from geobingan_sync.alert_state import AlertState
from geobingan_sync.steps.record_sync_result import notify_sync_outcome

T0 = datetime(2026, 9, 15, 8, 0)


def _run(checks, st, now, sent):
    return health_check.run_health_check(
        checks=checks, notify=True, alert_state=st, now=now,
        send=lambda t, b, m: sent.append((t, b, m)) or True)


def test_one_comment_per_run_and_mention_only_on_error(tmp_path):
    st = AlertState(tmp_path / 's.json'); sent = []
    checks = [('上傳暫停', lambda: ('warning', '已暫停 1 天')),
              ('JWT Token', lambda: ('error', '已過期')),
              ('磁碟空間', lambda: ('ok', '11GB'))]
    issues, events = _run(checks, st, T0, sent)
    assert len(issues) == 2 and len(sent) == 1
    title, body, mention = sent[0]
    assert mention is True and '上傳暫停' in body and 'JWT Token' in body


def test_identical_next_day_sends_nothing(tmp_path):
    st = AlertState(tmp_path / 's.json'); sent = []
    checks = [('上傳暫停', lambda: ('warning', '已暫停'))]
    _run(checks, st, T0, sent)
    _run(checks, st, T0 + timedelta(days=1), sent)
    assert len(sent) == 1


def test_error_recovery_still_reaches_person(tmp_path):
    """error 恢復＝needs_attention True → 走 Email；否則恢復通知到不了人（review P2）。"""
    st = AlertState(tmp_path / 's.json'); sent = []
    _run([('JWT Token', lambda: ('error', '已過期'))], st, T0, sent)
    _run([('JWT Token', lambda: ('ok', '剩 7 天'))], st, T0 + timedelta(days=1), sent)
    assert len(sent) == 2 and sent[1][2] is True and '已恢復' in sent[1][1]


def test_warning_recovery_does_not_disturb(tmp_path):
    st = AlertState(tmp_path / 's.json'); sent = []
    _run([('上傳暫停', lambda: ('warning', '已暫停'))], st, T0, sent)
    _run([('上傳暫停', lambda: ('ok', '未暫停'))], st, T0 + timedelta(days=1), sent)
    assert len(sent) == 2 and sent[1][2] is False


def test_check_exception_becomes_error(tmp_path):
    st = AlertState(tmp_path / 's.json'); sent = []
    def boom(): raise RuntimeError('api down')
    _run([('API 連線', boom)], st, T0, sent)
    assert sent and sent[0][2] is True and 'api down' in sent[0][1]


def test_sync_failure_then_recovery(tmp_path):
    st = AlertState(tmp_path / 's.json'); sent = []
    send = lambda t, b, m: sent.append((t, b, m))
    send = lambda t, b, m: sent.append((t, b, m)) or True
    notify_sync_outcome('failure', 'No space left', alert_state=st, now=T0, send=send)
    notify_sync_outcome('failure', 'No space left', alert_state=st, now=T0 + timedelta(days=1), send=send)
    notify_sync_outcome('success', '', alert_state=st, now=T0 + timedelta(days=2), send=send)
    assert [s[2] for s in sent] == [True, True]            # 失敗與恢復都要送到人；連日失敗不重發
    assert 'No space left' in sent[0][1] and '已恢復' in sent[1][1]


def test_dry_run_does_not_persist_state(tmp_path):
    path = tmp_path / 's.json'; st = AlertState(path); sent = []
    checks = [('上傳暫停', lambda: ('warning', '已暫停'))]
    health_check.run_health_check(checks=checks, notify=False, alert_state=st, now=T0,
                                  send=lambda t, b, m: sent.append(1))
    assert not path.exists() and sent == []          # 乾跑：不寫檔、不發
    _run(checks, st, T0, sent)
    assert path.exists() and len(sent) == 1           # 真跑才 seed + 發


def test_send_failure_does_not_commit_and_refires(tmp_path):
    """review P1：ClickUp 未送達 → 狀態不落地，下一輪同一事件重發。"""
    path = tmp_path / 's.json'; st = AlertState(path)
    checks = [('JWT Token', lambda: ('error', '已過期'))]
    calls = []
    health_check.run_health_check(checks=checks, notify=True, alert_state=st, now=T0,
                                  send=lambda t, b, m: calls.append(1) or False)
    assert not path.exists() and len(calls) == 1
    health_check.run_health_check(checks=checks, notify=True, alert_state=st, now=T0 + timedelta(hours=1),
                                  send=lambda t, b, m: calls.append(1) or True)
    assert path.exists() and len(calls) == 2           # 重試並成功後才 commit


def test_health_and_sync_interleave_without_false_recovery(tmp_path):
    """review P1：health 與 sync 交錯執行，不互相誤發 ✅。"""
    hc = AlertState(tmp_path / 'hc.json'); sy = AlertState(tmp_path / 'sy.json'); sent = []
    ok = lambda t, b, m: sent.append((t, b, m)) or True
    _run([('上傳暫停', lambda: ('warning', 'p'))], hc, T0, sent)
    notify_sync_outcome('success', '', alert_state=sy, now=T0 + timedelta(hours=2), send=ok)
    _run([('上傳暫停', lambda: ('warning', 'p'))], hc, T0 + timedelta(days=1), sent)
    assert len(sent) == 1 and not any('已恢復' in b for _, b, _ in sent)
