"""budget：估算/門檻閘門/月累計/等級（預算守門）。"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.budget import estimate_cost, budget_gate, budget_level, monthly_gate, gate_and_reserve, MonthlyBudget, ReservationLedger


def test_estimate_and_gate():
    assert estimate_cost(178, 0.3) == 53.4
    ok, msg = budget_gate(15, 0.3, 15, yes=False)          # 夜間 15 份 = US$4.5 不觸發
    assert ok and 'US$4.50' in msg
    ok, msg = budget_gate(178, 0.3, 15, yes=False)         # 人為大批次被擋
    assert not ok and '超過確認門檻' in msg and '--yes' in msg
    ok, _ = budget_gate(178, 0.3, 15, yes=True)            # 明確確認才放行
    assert ok


def test_budget_level_thresholds():
    assert budget_level(10, 100)[0] == 'ok'
    assert budget_level(70, 100)[0] == 'warning'
    assert budget_level(90, 100)[0] == 'error'
    assert budget_level(50, 0)[0] == 'ok'                  # 未設上限不告警


def test_monthly_budget_accumulates_and_rolls_over(tmp_path):
    mb = MonthlyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    sep = datetime(2026, 9, 14, 10, 0)
    assert mb.add(178, now=sep)['est_usd'] == 53.4
    d = mb.add(10, now=datetime(2026, 9, 15, 10, 0))
    assert d['uploaded'] == 188 and d['month'] == '2026-09'
    d = mb.load(now=datetime(2026, 10, 1, 10, 0))          # 跨月歸零
    assert d['uploaded'] == 0 and d['month'] == '2026-10'
    assert mb.add(3, now=datetime(2026, 10, 1, 10, 0))['uploaded'] == 3


def test_monthly_gate_trims_nightly_to_remaining_budget():
    """review P1：本月 US$98、本次 15 份（US$4.5）→ 投影 102.5 > 100，自動裁切為 6 份。"""
    allowed, msg = monthly_gate(15, 98.0, 100.0, 0.3, yes=False)
    assert allowed == 6 and '自動裁切' in msg


def test_monthly_gate_blocks_when_budget_exhausted():
    allowed, msg = monthly_gate(15, 100.0, 100.0, 0.3, yes=False)
    assert allowed == 0 and '已擋下' in msg
    allowed, _ = monthly_gate(15, 99.9, 100.0, 0.3, yes=False)     # 剩 0.1，不足 1 份
    assert allowed == 0


def test_monthly_gate_passes_within_budget_and_yes_overrides():
    assert monthly_gate(15, 50.0, 100.0, 0.3, yes=False)[0] == 15
    allowed, msg = monthly_gate(15, 98.0, 100.0, 0.3, yes=True)
    assert allowed == 15 and '--yes' in msg
    assert monthly_gate(15, 500.0, 0, 0.3, yes=False)[0] == 15          # 未設上限


def test_reserve_charges_upfront_and_release_refunds_unused(tmp_path):
    """review P1：先預留後退還；中斷時已成功份數不會漏記。"""
    mb = MonthlyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved, msg, d = mb.reserve(15, 100.0, 0.3, yes=False)
    assert reserved == 15 and d['uploaded'] == 15                  # 預留即計入
    # 模擬跑到一半中斷（成功 4 份就掛了、沒退還）：帳上仍是 15，只會多算不會少算
    assert mb.load()['uploaded'] == 15
    # 正常結束：退還 15-4=11 → 帳上 4
    assert mb.release(15 - 4)['uploaded'] == 4
    assert mb.release(0)['uploaded'] == 4 and mb.release(-3)['uploaded'] == 4   # 非正數不動作


def test_reserve_trims_and_blocks_using_charged_balance(tmp_path):
    mb = MonthlyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    mb.set_uploaded(327)                                            # 98.1 美元
    reserved, msg, d = mb.reserve(15, 100.0, 0.3, yes=False)
    assert reserved == 6 and d['uploaded'] == 333 and '自動裁切' in msg
    reserved, msg, _ = mb.reserve(15, 100.0, 0.3, yes=False)       # 餘額 0.1，不足 1 份
    assert reserved == 0 and '已擋下' in msg


def test_reserve_is_atomic_across_processes(tmp_path):
    """review P1：8 個程序同時預留（預算只容 10 份），總放行必須恰好 10、不多不少、無檔案覆蓋。"""
    import subprocess, sys as _sys, os as _os
    path = tmp_path / 'b.json'
    code = (
        "import sys; sys.path.insert(0, %r); from geobingan_sync.budget import MonthlyBudget; "
        "r,_,_ = MonthlyBudget(%r, cost_per_report=0.3).reserve(5, 3.0, 0.3, yes=False); print(r)"
    ) % (_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), str(path))
    procs = [subprocess.Popen([_sys.executable, '-c', code], stdout=subprocess.PIPE, text=True) for _ in range(8)]
    allowed = [int(p.communicate()[0].strip().splitlines()[-1]) for p in procs]
    assert sum(allowed) == 10, allowed
    assert MonthlyBudget(path, cost_per_report=0.3).load()['uploaded'] == 10
    assert not list(tmp_path.glob('*.tmp'))                        # 無殘留暫存檔


class _FakeMB:
    """預留時會「放行很多」的假帳本，用來證明門檻不可能被預留結果放大。"""
    def __init__(self): self.calls = []
    def reserve(self, n, budget, cost, yes):
        self.calls.append(n); return n, '假預留全數放行', {'month': '2026-09'}


def test_gate_checks_original_count_before_any_reservation():
    """review TOCTOU：100 份未帶 --yes 必須在預留之前就被擋，reserve 不得被呼叫。"""
    mb = _FakeMB()
    allowed, msgs, blocked, _ = gate_and_reserve(mb, 100, 100.0, 0.3, 15.0, yes=False)
    assert allowed == 0 and blocked and '超過確認門檻' in blocked
    assert mb.calls == []                                   # 尚未預留，不需退還
    allowed, msgs, blocked, _ = gate_and_reserve(mb, 15, 100.0, 0.3, 15.0, yes=False)
    assert allowed == 15 and blocked is None and mb.calls == [15]
    allowed, _, blocked, _ = gate_and_reserve(mb, 100, 100.0, 0.3, 15.0, yes=True)   # --yes 才放行大批次
    assert allowed == 100 and blocked is None


def test_gate_and_reserve_with_real_ledger_trims(tmp_path):
    mb = MonthlyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    mb.set_uploaded(327)                                    # 已用 98.1
    allowed, msgs, blocked, _ = gate_and_reserve(mb, 15, 100.0, 0.3, 15.0, yes=False)
    assert allowed == 6 and blocked is None                 # 門檻對 15 份（4.5 美元）通過，預留裁成 6
    allowed, msgs, blocked, _ = gate_and_reserve(mb, 15, 100.0, 0.3, 15.0, yes=False)
    assert allowed == 0 and '已擋下' in blocked              # 餘額耗盡


class _LedgerMB:
    def __init__(self): self.releases = []
    def release(self, n, now=None, month=None): self.releases.append(n); return {'uploaded': -1}
    def load(self): return {'uploaded': -1}


def test_ledger_refunds_only_definite_zero_cost_failures():
    mb = _LedgerMB(); L = ReservationLedger(mb, reserved=4)
    # 協定：begin_item 在 POST 前呼叫；下載失敗的項目不會呼叫 begin_item
    L.begin_item(); L.settle({'success': True})
    L.settle({'success': False, 'error': 'download_failed'})          # 未 begin → 不即時退，留給 close
    L.begin_item(); L.settle({'success': False, 'error': 'rejected'}) # 明確拒絕 → 即時退 1
    L.begin_item(); L.settle({'success': False, 'error': 'unknown'})  # 結果不明 → 保留
    L.close()                                                          # 4 預留 − 3 已嘗試 = 退 1（下載失敗那份）
    assert mb.releases == [1, 1]


def test_ledger_interrupt_keeps_attempted_items_charged():
    """review P1：API 成功後、settle 前中斷 → 該份不退；只退從未嘗試的。"""
    mb = _LedgerMB(); L = ReservationLedger(mb, reserved=6)
    L.begin_item(); L.settle({'success': True})
    L.begin_item(); L.settle({'success': True})
    L.begin_item()                                    # 第 3 份：API 已受理但尚未 settle 就中斷
    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        L.close()
    assert mb.releases == [3]                         # 只退 6-3=3 份從未嘗試的；第 3 份保留在帳上


def test_ledger_normal_completion_with_real_ledger(tmp_path):
    mb = MonthlyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    reserved, _, _ = mb.reserve(6, 100.0, 0.3, yes=False)
    L = ReservationLedger(mb, reserved)
    for r in ({'success': True},) * 4 + ({'success': False, 'error': 'rejected'}, {'success': False, 'error': 'unknown'}):
        L.begin_item(); L.settle(r)
    assert L.close()['uploaded'] == 5                 # 6 預留 − 1 明確拒絕 = 5（未知那份保留）


def test_release_from_old_month_does_not_touch_new_month(tmp_path):
    """review P1 跨月：9 月預留，10 月已有其他程序預留，9 月 ledger 結算不得改動 10 月數字。"""
    mb = MonthlyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    sep = datetime(2026, 9, 30, 23, 59); oct1 = datetime(2026, 10, 1, 0, 5)
    reserved_a, _, data_a = mb.reserve(15, 100.0, 0.3, yes=False, now=sep)
    ledger_a = ReservationLedger(mb, reserved_a, month=data_a['month'])
    assert ledger_a.month == '2026-09'
    reserved_b, _, data_b = mb.reserve(15, 100.0, 0.3, yes=False, now=oct1)     # 程序 B：10 月帳本
    assert data_b['month'] == '2026-10' and data_b['uploaded'] == 15
    # 程序 A 在 10 月收到明確拒絕、並結束（3 份未嘗試）→ 對 10 月帳本必須 no-op
    ledger_a.begin_item(); ledger_a.settle({'success': False, 'error': 'rejected'})
    ledger_a.begin_item(); ledger_a.settle({'success': True})
    mb.release(20, now=oct1, month='2026-09')       # 直接呼叫也 no-op
    ledger_a.close()                                 # 退還 13 份未嘗試 → 對 10 月帳本必須 no-op
    raw = mb._read_raw()                             # 檔案實際內容才是不變量（close 的回傳只是當下月份的顯示視圖）
    assert raw['month'] == '2026-10' and raw['uploaded'] == 15


def test_release_same_month_before_rollover_applies(tmp_path):
    mb = MonthlyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    sep = datetime(2026, 9, 30, 23, 59)
    reserved, _, data = mb.reserve(15, 100.0, 0.3, yes=False, now=sep)
    L = ReservationLedger(mb, reserved, month=data['month'])
    L.begin_item(); L.settle({'success': False, 'error': 'rejected'})
    assert mb.load(now=sep)['uploaded'] == 14                                    # 同月正常扣減
    assert L.close()['uploaded'] == 0                                            # 退還 14 份未嘗試


def test_begin_item_refuses_after_month_rollover_and_new_month_ledger_counts_later_sends(tmp_path):
    """review P1：9 月預留後跨到 10 月，不得再用 9 月預留送 API；之後送出的份數要進 10 月帳本。"""
    mb = MonthlyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    sep = datetime(2026, 9, 30, 23, 59); oct1 = datetime(2026, 10, 1, 0, 5)
    reserved, _, data = mb.reserve(15, 100.0, 0.3, yes=False, now=sep)
    L = ReservationLedger(mb, reserved, month=data['month'])
    assert L.begin_item(now=sep) is True                      # 同月：可送
    sent_in_sep = 1
    assert L.begin_item(now=oct1) is False                    # 跨月：拒絕，attempted 不增加
    assert L.attempted == sent_in_sep
    L.close()                                                 # 舊月未用 14 份：檔案仍是 9 月 → 扣 9 月，不動 10 月
    assert mb._read_raw()['month'] == '2026-09' and mb._read_raw()['uploaded'] == 1
    # 下次執行（10 月）重新預留剩餘 14 份 → 進 10 月帳本
    reserved2, _, data2 = mb.reserve(14, 100.0, 0.3, yes=False, now=oct1)
    assert data2['month'] == '2026-10' and data2['uploaded'] == 14 and reserved2 == 14


def test_begin_item_without_month_is_legacy_always_true():
    L = ReservationLedger(_LedgerMB(), reserved=3)
    assert L.begin_item(now=datetime(2030, 1, 1)) is True and L.attempted == 1


def test_month_guard_right_before_post_blocks_after_slow_download(tmp_path, monkeypatch):
    """review P1：下載開始於 9 月、完成時已 10 月 → upload_to_geobingan 不得被呼叫、attempted 不增、
    不寫 error/歷史；下次能在 10 月重新預留。"""
    import geobingan_sync.steps.upload_pdfs as up
    mb = MonthlyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    sep = datetime(2026, 9, 30, 23, 59, 59); oct1 = datetime(2026, 10, 1, 0, 3)
    reserved, _, data = mb.reserve(15, 100.0, 0.3, yes=False, now=sep)
    clock = {'now': sep}
    L = ReservationLedger(mb, reserved, month=data['month'], clock=lambda: clock['now'])
    posted = []
    def slow_download(service, fid, name):
        clock['now'] = oct1                         # 下載期間跨月
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
    L.close()                                       # 9 月未嘗試 15 份：檔案仍 9 月 → 退 9 月，不動 10 月
    assert mb._read_raw()['month'] == '2026-09' and mb._read_raw()['uploaded'] == 0
    reserved2, _, data2 = mb.reserve(15, 100.0, 0.3, yes=False, now=oct1)
    assert data2['month'] == '2026-10' and reserved2 == 15


def test_month_guard_same_month_allows_post(tmp_path, monkeypatch):
    import geobingan_sync.steps.upload_pdfs as up
    mb = MonthlyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    sep = datetime(2026, 9, 30, 23, 0)
    reserved, _, data = mb.reserve(2, 100.0, 0.3, yes=False, now=sep)
    L = ReservationLedger(mb, reserved, month=data['month'], clock=lambda: sep)
    posted = []
    monkeypatch.setattr(up, 'download_pdf', lambda s, f, n: b'%PDF')
    monkeypatch.setattr(up, 'upload_to_geobingan', lambda c, n, f: posted.append(n) or {'id': 'x'})
    monkeypatch.setattr(up, 'save_state', lambda state: None)
    monkeypatch.setattr(up, 'add_to_history', lambda uid: None)
    r = up.process_single_pdf(None, {'id': 'f', 'name': 'a.pdf', 'folder_name': 'X'}, {'uploaded_files': [], 'errors': []}, 1, 1,
                              before_upload=L.begin_item)
    assert r['success'] and posted == ['a.pdf'] and L.attempted == 1
