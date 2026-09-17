"""解析預算守門（日模型）。

後端真實限制＝每日 US$20（2026-09-16 確認），上傳與手動 retry-parse 吃同一份
日額度。此檔涵蓋：估算與單次門檻、日上限裁切、原子預留與退還、跨日保護、
POST 前守門、以及重推與上傳共用額度。
"""
import json
import os
import sys

import pytest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.budget import (estimate_cost, budget_gate, budget_level, daily_gate,
                                   gate_and_reserve, DailyBudget, ReservationLedger)

# 一律 timezone-aware UTC：day_key() 現在直接拒絕 naive（review P1）
UTC = timezone.utc
D0 = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)          # 基準日
D0_LATE = datetime(2026, 9, 17, 23, 59, tzinfo=UTC)
D1 = datetime(2026, 9, 18, 0, 5, tzinfo=UTC)           # 隔日（UTC）
DAY0, DAY1 = '2026-09-17', '2026-09-18'


# ---------- 估算與單次門檻 ----------

def test_estimate_and_gate():
    assert estimate_cost(178, 0.3) == 53.4
    ok, msg = budget_gate(15, 0.3, 15, yes=False)          # 夜間 15 份＝US$4.5，不觸發
    assert ok and 'US$4.50' in msg
    ok, msg = budget_gate(178, 0.3, 15, yes=False)         # 人為大批次被擋
    assert not ok and '超過確認門檻' in msg and '--yes' in msg
    assert budget_gate(178, 0.3, 15, yes=True)[0]          # 明確確認才放行


def test_budget_level_thresholds():
    assert budget_level(4.5, 20)[0] == 'ok'                # 22%
    assert budget_level(14, 20)[0] == 'warning'            # 70%
    assert budget_level(18, 20)[0] == 'error'              # 90%
    assert budget_level(50, 0)[0] == 'ok'                  # 未設上限不告警


# ---------- 日上限閘門 ----------

def test_daily_gate_trims_to_remaining_budget():
    """今日已用 US$16.5、再來 15 份 → 投影 21 > 20，裁切為剩餘可容納的 11 份。"""
    allowed, msg = daily_gate(15, 16.5, 20.0, 0.3)
    assert allowed == 11 and '自動裁切' in msg


def test_daily_gate_blocks_when_exhausted():
    allowed, msg = daily_gate(15, 20.0, 20.0, 0.3)
    assert allowed == 0 and '已擋下' in msg
    assert daily_gate(15, 19.9, 20.0, 0.3)[0] == 0                # 剩 0.1 不足 1 份


def test_daily_gate_passes_and_override_bypasses():
    assert daily_gate(15, 4.0, 20.0, 0.3)[0] == 15
    allowed, msg = daily_gate(15, 18.0, 20.0, 0.3, override=True, override_reason='後端已加碼')
    assert allowed == 15 and '--override-daily-budget' in msg and '後端已加碼' in msg
    assert daily_gate(15, 500.0, 0, 0.3)[0] == 15                  # 未設上限


# ---------- 帳本：累加、跨日歸零、重推共用 ----------

def test_accumulates_within_day_and_resets_next_day(tmp_path):
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    assert mb.add(40, now=D0)['est_usd'] == 12.0
    d = mb.add(10, now=D0_LATE)
    assert d['uploaded'] == 50 and d['day'] == DAY0
    d = mb.load(now=D1)
    assert d['units'] == 0 and d['day'] == DAY1                     # 跨日歸零
    assert mb.add(3, now=D1)['uploaded'] == 3


def test_retries_and_uploads_share_the_same_daily_budget(tmp_path):
    """重推與上傳吃同一份日額度；只算上傳會讓重推的消耗對守門隱形。"""
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    mb.record_retry(55, now=D0)                                     # 手動重推 55 份
    d = mb.load(now=D0)
    assert (d['retried'], d['units'], d['est_usd']) == (55, 55, 16.5)
    allowed, msg, data = mb.reserve(15, 20.0, 0.3, now=D0)
    assert allowed == 11 and '自動裁切' in msg                       # 剩 US$3.5 → 11 份
    assert data['uploaded'] == 11 and data['units'] == 66


def test_old_month_format_file_is_reset(tmp_path):
    """舊的月格式帳本檔要被視為非今日、自動歸零，不可誤當今日已用。"""
    p = tmp_path / 'b.json'
    p.write_text(json.dumps({'month': '2026-09', 'uploaded': 224, 'est_usd': 67.2}), encoding='utf-8')
    d = DailyBudget(p, cost_per_report=0.3).load(now=D0)
    assert d['day'] == DAY0 and d['units'] == 0 and d['est_usd'] == 0.0


# ---------- 原子預留與退還 ----------

def test_reserve_charges_upfront_and_release_refunds_unused(tmp_path):
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved, _, d = mb.reserve(15, 20.0, 0.3, now=D0)
    assert reserved == 15 and d['uploaded'] == 15                    # 預留即計入
    assert mb.load(now=D0)['uploaded'] == 15                         # 中途中斷：帳上仍在（只會多算）
    assert mb.release(15 - 4, now=D0, day=DAY0)['uploaded'] == 4     # 正常結束退還未用
    assert mb.release(0, now=D0)['uploaded'] == 4


def test_reserve_is_atomic_across_processes(tmp_path):
    """8 個程序同時預留（額度只容 10 份）→ 總放行恰為 10、無殘留暫存檔。"""
    import subprocess
    path = tmp_path / 'b.json'
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys; sys.path.insert(0, %r); from geobingan_sync.budget import DailyBudget; "
        "r,_,_ = DailyBudget(%r, cost_per_report=0.3).reserve(5, 3.0, 0.3); print(r)"
    ) % (root, str(path))
    procs = [subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
             for _ in range(8)]
    allowed = [int(p.communicate()[0].strip().splitlines()[-1]) for p in procs]
    assert sum(allowed) == 10, allowed
    assert DailyBudget(path, cost_per_report=0.3).load()['uploaded'] == 10
    assert not list(tmp_path.glob('*.tmp'))


# ---------- 門檻先於預留（TOCTOU） ----------

class _FakeMB:
    """預留時會「放行很多」的假帳本，證明門檻不可能被預留結果放大。"""
    def __init__(self): self.calls = []
    def reserve(self, n, budget, cost, override=False, override_reason=''):
        self.calls.append(n); return n, '假預留全數放行', {'day': DAY0}


def test_gate_checks_original_count_before_any_reservation():
    mb = _FakeMB()
    allowed, _, blocked, _ = gate_and_reserve(mb, 100, 20.0, 0.3, 15.0, yes=False)
    assert allowed == 0 and blocked and '超過確認門檻' in blocked
    assert mb.calls == []                                            # 尚未預留，不需退還
    allowed, _, blocked, _ = gate_and_reserve(mb, 15, 20.0, 0.3, 15.0, yes=False)
    assert allowed == 15 and blocked is None and mb.calls == [15]
    allowed, _, blocked, _ = gate_and_reserve(mb, 100, 20.0, 0.3, 15.0, yes=True)
    assert allowed == 100 and blocked is None


def test_gate_and_reserve_with_real_ledger(tmp_path):
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    mb.record_retry(55, now=D0)                                      # 今日已用 US$16.5
    allowed, _, blocked, day = gate_and_reserve(mb, 15, 20.0, 0.3, 15.0, yes=False)
    assert allowed == 11 and blocked is None and day == DAY0
    allowed, _, blocked, _ = gate_and_reserve(mb, 15, 20.0, 0.3, 15.0, yes=False)
    assert allowed == 0 and '已擋下' in blocked                       # 額度耗盡


# ---------- ReservationLedger ----------

class _LedgerMB:
    def __init__(self): self.releases = []; self.kinds = []
    def release(self, n, now=None, day=None, kind='uploaded'):
        self.releases.append(n); self.kinds.append(kind); return {'uploaded': -1}
    def load(self): return {'uploaded': -1}


def test_ledger_refunds_only_definite_zero_cost_failures():
    mb = _LedgerMB(); L = ReservationLedger(mb, reserved=4)
    L.begin_item(); L.settle({'success': True})
    L.settle({'success': False, 'error': 'download_failed'})          # 未 begin → 留給 close
    L.begin_item(); L.settle({'success': False, 'error': 'rejected'}) # 4xx → 即時退 1
    L.begin_item(); L.settle({'success': False, 'error': 'unknown'})  # 結果不明 → 保留
    L.close()                                                          # 4−3＝退 1
    assert mb.releases == [1, 1]
    assert mb.kinds == ['uploaded', 'uploaded']                        # 退還記回同一欄


def test_ledger_interrupt_keeps_attempted_items_charged():
    mb = _LedgerMB(); L = ReservationLedger(mb, reserved=6)
    L.begin_item(); L.settle({'success': True})
    L.begin_item(); L.settle({'success': True})
    L.begin_item()                                                     # API 已受理、settle 前中斷
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        L.close()
    assert mb.releases == [3]                                          # 只退未嘗試的 3 份


def test_ledger_normal_completion_with_real_ledger(tmp_path):
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved, _, _ = mb.reserve(6, 20.0, 0.3, now=D0)
    L = ReservationLedger(mb, reserved, day=DAY0)
    for r in ({'success': True},) * 4 + ({'success': False, 'error': 'rejected'},
                                         {'success': False, 'error': 'unknown'}):
        L.begin_item(now=D0); L.settle(r)
    assert L.close()['uploaded'] == 5                                  # 6 − 1 明確拒絕


# ---------- 跨日保護 ----------

def test_release_from_previous_day_does_not_touch_today(tmp_path):
    """昨日的預留跨日退還時，不得扣到今日（其他程序）的額度。"""
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved_a, _, data_a = mb.reserve(15, 60.0, 0.3, now=D0_LATE)
    ledger_a = ReservationLedger(mb, reserved_a, day=data_a['day'])
    assert ledger_a.day == DAY0

    _, _, data_b = mb.reserve(15, 60.0, 0.3, now=D1)        # 程序 B：新的一天
    assert data_b['day'] == DAY1 and data_b['uploaded'] == 15

    ledger_a.begin_item(now=D0_LATE); ledger_a.settle({'success': False, 'error': 'rejected'})
    mb.release(20, now=D1, day=DAY0)                                   # 直接呼叫也 no-op
    ledger_a.close()
    raw = mb._read_raw()
    assert raw['day'] == DAY1 and raw['uploaded'] == 15                # 今日數字未被動


def test_release_same_day_applies(tmp_path):
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved, _, data = mb.reserve(15, 60.0, 0.3, now=D0)
    L = ReservationLedger(mb, reserved, day=data['day'])
    L.begin_item(now=D0); L.settle({'success': False, 'error': 'rejected'})
    assert mb.load(now=D0)['uploaded'] == 14                           # 同日正常扣減
    assert L.close()['uploaded'] == 0                                  # 退還 14 份未嘗試


def test_begin_item_refuses_after_day_rollover(tmp_path):
    """跨日後不得再用昨日預留送出；之後的份數要進新一天的帳本。"""
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved, _, data = mb.reserve(15, 60.0, 0.3, now=D0_LATE)
    L = ReservationLedger(mb, reserved, day=data['day'])
    assert L.begin_item(now=D0_LATE) is True
    assert L.begin_item(now=D1) is False and L.attempted == 1          # 跨日拒絕、不計入
    L.close()
    assert mb._read_raw()['day'] == DAY0 and mb._read_raw()['uploaded'] == 1
    reserved2, _, data2 = mb.reserve(14, 60.0, 0.3, now=D1)
    assert data2['day'] == DAY1 and reserved2 == 14


def test_begin_item_without_day_is_legacy_always_true():
    L = ReservationLedger(_LedgerMB(), reserved=3)
    assert L.begin_item(now=datetime(2030, 1, 1)) is True and L.attempted == 1


# ---------- POST 前一刻守門 ----------

def test_day_guard_right_before_post_blocks_after_slow_download(tmp_path, monkeypatch):
    """下載開始於 9/17、完成時已 9/18 → 不得 POST、attempted 不增、不寫 error/歷史。"""
    import geobingan_sync.steps.upload_pdfs as up
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved, _, data = mb.reserve(15, 60.0, 0.3, now=D0_LATE)
    clock = {'now': D0_LATE}
    L = ReservationLedger(mb, reserved, day=data['day'], clock=lambda: clock['now'])
    posted = []

    def slow_download(service, fid, name):
        clock['now'] = D1                                              # 下載期間跨日
        return b'%PDF'
    monkeypatch.setattr(up, 'download_pdf', slow_download)
    monkeypatch.setattr(up, 'upload_to_geobingan', lambda c, n, f: posted.append(n) or {'id': 'x'})
    monkeypatch.setattr(up, 'save_state', lambda state: None)
    monkeypatch.setattr(up, 'add_to_history', lambda uid: None)

    state = {'uploaded_files': [], 'errors': []}
    r = up.process_single_pdf(None, {'id': 'f', 'name': 'a.pdf', 'folder_name': 'X'}, state, 1, 1,
                              before_upload=L.begin_item)
    assert r['error'] == 'month_rolled_over' and posted == [] and L.attempted == 0
    assert state['uploaded_files'] == [] and state['errors'] == []
    L.close()
    assert mb._read_raw()['day'] == DAY0 and mb._read_raw()['uploaded'] == 0
    reserved2, _, data2 = mb.reserve(15, 60.0, 0.3, now=D1)
    assert data2['day'] == DAY1 and reserved2 == 15


def test_day_guard_same_day_allows_post(tmp_path, monkeypatch):
    import geobingan_sync.steps.upload_pdfs as up
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved, _, data = mb.reserve(2, 20.0, 0.3, now=D0)
    L = ReservationLedger(mb, reserved, day=data['day'], clock=lambda: D0)
    posted = []
    monkeypatch.setattr(up, 'download_pdf', lambda s, f, n: b'%PDF')
    monkeypatch.setattr(up, 'upload_to_geobingan', lambda c, n, f: posted.append(n) or {'id': 'x'})
    monkeypatch.setattr(up, 'save_state', lambda state: None)
    monkeypatch.setattr(up, 'add_to_history', lambda uid: None)
    r = up.process_single_pdf(None, {'id': 'f', 'name': 'a.pdf', 'folder_name': 'X'},
                              {'uploaded_files': [], 'errors': []}, 1, 1, before_upload=L.begin_item)
    assert r['success'] and posted == ['a.pdf'] and L.attempted == 1


# ---------- 重推與上傳共用同一份日額度（PR #85 review P1） ----------

def test_reserve_retry_counts_into_retried_not_uploaded(tmp_path):
    """重推預留記在 retried，但一樣吃掉 units，讓後續上傳看得到。"""
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved, _, d = mb.reserve_retry(55, 20.0, 0.3, now=D0)
    assert reserved == 55
    assert d['retried'] == 55 and d['uploaded'] == 0                 # 分欄記帳
    assert d['units'] == 55                                          # 但共用同一份總量
    allowed, msg, d2 = mb.reserve(15, 20.0, 0.3, now=D0)
    assert allowed == 11 and '自動裁切' in msg                        # 剩 US$3.5 → 只放行 11 份
    assert d2['units'] == 66


def test_retry_release_refunds_retried_column(tmp_path):
    """重推退還要回到 retried，不可錯記成上傳額度。"""
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    mb.reserve_retry(10, 20.0, 0.3, now=D0)
    d = mb.release(4, now=D0, day=DAY0, kind='retried')
    assert d['retried'] == 6 and d['uploaded'] == 0 and d['units'] == 6


def test_retry_blocked_when_daily_budget_exhausted(tmp_path):
    """上傳已用滿當日額度 → 重推必須被擋下（而不是照送、事後才發現超支）。"""
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    mb.reserve(66, 20.0, 0.3, override=True, now=D0)                      # 66 × 0.3 = US$19.8
    allowed, msg, _ = mb.reserve_retry(20, 20.0, 0.3, now=D0)
    assert allowed == 0 and ('不足' in msg or '耗盡' in msg), msg


def test_gate_and_reserve_routes_retry_to_retried(tmp_path):
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved, msgs, blocked, day = gate_and_reserve(mb, 10, 20.0, 0.3, 15.0, False,
                                                    kind='retried')
    assert reserved == 10 and not blocked
    assert mb.load()['retried'] == 10 and mb.load()['uploaded'] == 0


def test_retry_ledger_refunds_only_unattempted_into_retried(tmp_path):
    """重推沿用保守結算：4xx 明確拒絕退還，未嘗試的退還，其餘保守保留。"""
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    mb.reserve_retry(5, 20.0, 0.3, now=D0)
    led = ReservationLedger(mb, 5, day=DAY0, clock=lambda: D0, kind='retried')
    led.begin_item(); led.settle({'success': False, 'error': 'rejected'})   # 4xx → 退 1
    led.begin_item()                                                        # 202 → 保留
    led.begin_item()                                                        # 5xx → 保守保留
    d = led.close()                                                         # 未嘗試 2 份 → 退還
    assert d['retried'] == 2 and d['uploaded'] == 0                         # 5 - 1 - 2 = 2


def test_retry_and_upload_never_exceed_daily_budget_across_processes(tmp_path):
    """8 個程序（4 重推 + 4 上傳）同時搶只夠 10 份的日額度 → 總放行恰為 10。

    這正是 --record-retry 事後記帳擋不住的競態：重推已送出但還沒記帳時，
    上傳讀到用量偏低而照常預留，兩者合計超過後端日上限（2026-09-14 事故）。
    """
    import subprocess
    path = tmp_path / 'b.json'
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmpl = (
        "import sys; sys.path.insert(0, %r); from geobingan_sync.budget import DailyBudget; "
        "mb = DailyBudget(%r, cost_per_report=0.3); "
        "r,_,_ = mb.%s(5, 3.0, 0.3); print(r)"
    )
    procs = [subprocess.Popen([sys.executable, '-c',
                               tmpl % (root, str(path),
                                       'reserve_retry' if i % 2 else 'reserve')],
                              stdout=subprocess.PIPE, text=True)
             for i in range(8)]
    allowed = [int(p.communicate()[0].strip().splitlines()[-1]) for p in procs]
    assert sum(allowed) == 10, allowed                               # 總放行不超過日額度
    d = DailyBudget(path, cost_per_report=0.3).load()
    assert d['uploaded'] + d['retried'] == 10 and d['units'] == 10   # 兩欄合計才是額度
    assert not list(tmp_path.glob('*.tmp'))
    # 註：不斷言「兩欄都 > 0」——先搶到鎖的兩個程序可能同類，額度即被吃光。
    #     「兩條路徑真的共用同一份帳本」由下面的確定性測試保證。


def test_retry_and_upload_draw_from_the_same_ledger(tmp_path):
    """一個上傳、一個重推各要 5 份，額度恰好 10 份 → 兩者都拿到且合計用盡。

    與上面的 8 程序測試互補：這裡份數確定，證明兩條路徑吃的是同一份 units，
    而不是各自一份（各自一份時合計會是 20，後端就會被打爆）。
    """
    import subprocess
    path = tmp_path / 'b.json'
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmpl = (
        "import sys; sys.path.insert(0, %r); from geobingan_sync.budget import DailyBudget; "
        "r,_,_ = DailyBudget(%r, cost_per_report=0.3).%s(5, 3.0, 0.3); print(r)"
    )
    procs = [subprocess.Popen([sys.executable, '-c', tmpl % (root, str(path), fn)],
                              stdout=subprocess.PIPE, text=True)
             for fn in ('reserve', 'reserve_retry')]
    allowed = [int(p.communicate()[0].strip().splitlines()[-1]) for p in procs]
    assert allowed == [5, 5], allowed
    d = DailyBudget(path, cost_per_report=0.3).load()
    assert d['uploaded'] == 5 and d['retried'] == 5 and d['units'] == 10
    # 額度已用盡：第三個請求一份都拿不到
    assert DailyBudget(path, cost_per_report=0.3).reserve(1, 3.0, 0.3)[0] == 0


# ---------- 日界對齊 provider（UTC） ----------

def test_day_key_uses_utc_not_local_time():
    """台北午夜（UTC+8）不可當成換日——否則比後端提早 8 小時放行整份額度。"""
    from geobingan_sync.budget import day_key
    tpe = timezone(timedelta(hours=8))
    assert day_key(datetime(2026, 9, 18, 0, 30, tzinfo=tpe)) == '2026-09-17'   # 仍屬前一 UTC 日
    assert day_key(datetime(2026, 9, 18, 8, 30, tzinfo=tpe)) == '2026-09-18'   # UTC 00:30 才換日


def test_day_key_rejects_naive_datetime():
    """naive datetime 必須當場 raise。

    先前版本把 naive 當成「已是 UTC」，而 production 每個呼叫點傳的都是
    `datetime.now()`＝台北本地時間，於是實際日界仍落在台北午夜，UTC 對齊形同
    虛設（review P1）。擋在型別上，比要求每個呼叫點自律可靠。
    """
    from geobingan_sync.budget import day_key
    with pytest.raises(ValueError, match='timezone-aware'):
        day_key(datetime(2026, 9, 17, 23, 0))


def test_production_clock_is_utc_aware():
    """utcnow() 是本模組唯一時鐘，必須帶時區，否則上面那道防線等於沒有。"""
    from geobingan_sync.budget import utcnow
    now = utcnow()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


def test_ledger_default_clock_is_utc_aware(tmp_path):
    """ReservationLedger 預設時鐘也要是 UTC-aware，否則 begin_item 會炸。"""
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved, _, data = mb.reserve(3, 20.0, 0.3)
    led = ReservationLedger(mb, reserved, day=data['day'])
    assert led.begin_item() is True                                  # 不得 raise


# ---------- --yes 不得放寬日上限（review P1） ----------

def test_yes_confirms_batch_size_but_never_bypasses_daily_cap(tmp_path):
    """`--yes` 只解單次門檻，日上限仍須強制裁切。

    55 份重推估 US$16.5，超過確認門檻 US$15 所以一定要帶 --yes；若 --yes 同時
    放寬日上限，今日已用 US$10 時就會放行全部 55 份、投影 US$26.5，直接突破後端
    每日 US$20 的硬限——本模組的核心保證會在每一次合法大批次上失效。
    """
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    mb.record_retry(34, now=D0)                                      # 今日已用 US$10.20
    reserved, msgs, blocked, _ = gate_and_reserve(
        mb, 55, 20.0, 0.3, 15.0, yes=True, kind='retried')           # 帶 --yes 過單次門檻
    assert blocked is None                                           # 單次門檻確實放行了
    assert reserved == 32, reserved                                  # 但日上限仍裁切：剩 US$9.8 → 32 份
    assert any('自動裁切' in m for m in msgs)
    d = mb.load(now=D0)
    assert d['units'] == 66 and d['est_usd'] <= 20.0                 # 絕不超過日上限


def test_override_flag_is_the_only_way_past_daily_cap(tmp_path):
    """真要超過後端額度，必須另外明講 override（排程不會帶）。"""
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    mb.record_retry(34, now=D0)
    reserved, msgs, blocked, _ = gate_and_reserve(
        mb, 55, 20.0, 0.3, 15.0, yes=True, kind='retried',
        override=True, override_reason='後端已臨時加碼')
    assert blocked is None and reserved == 55
    assert any('後端已臨時加碼' in m for m in msgs)                    # 理由要留在日誌裡


def test_override_alone_still_needs_yes_for_single_batch_threshold(tmp_path):
    """override 不代換 --yes：單次大批次仍要人確認，兩道閘各司其職。"""
    mb = DailyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved, _, blocked, _ = gate_and_reserve(
        mb, 55, 20.0, 0.3, 15.0, yes=False, kind='retried', override=True)
    assert reserved == 0 and blocked and '超過確認門檻' in blocked
    assert mb.load()['units'] == 0                                   # 擋在預留之前
