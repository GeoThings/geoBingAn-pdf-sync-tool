"""budget：估算/門檻閘門/月累計/等級（預算守門）。"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.budget import estimate_cost, budget_gate, budget_level, monthly_gate, gate_and_reserve, MonthlyBudget


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
        self.calls.append(n); return n, '假預留全數放行', {}


def test_gate_checks_original_count_before_any_reservation():
    """review TOCTOU：100 份未帶 --yes 必須在預留之前就被擋，reserve 不得被呼叫。"""
    mb = _FakeMB()
    allowed, msgs, blocked = gate_and_reserve(mb, 100, 100.0, 0.3, 15.0, yes=False)
    assert allowed == 0 and blocked and '超過確認門檻' in blocked
    assert mb.calls == []                                   # 尚未預留，不需退還
    allowed, msgs, blocked = gate_and_reserve(mb, 15, 100.0, 0.3, 15.0, yes=False)
    assert allowed == 15 and blocked is None and mb.calls == [15]
    allowed, _, blocked = gate_and_reserve(mb, 100, 100.0, 0.3, 15.0, yes=True)   # --yes 才放行大批次
    assert allowed == 100 and blocked is None


def test_gate_and_reserve_with_real_ledger_trims(tmp_path):
    mb = MonthlyBudget(tmp_path / 'b.json', cost_per_report=0.3)
    mb.set_uploaded(327)                                    # 已用 98.1
    allowed, msgs, blocked = gate_and_reserve(mb, 15, 100.0, 0.3, 15.0, yes=False)
    assert allowed == 6 and blocked is None                 # 門檻對 15 份（4.5 美元）通過，預留裁成 6
    allowed, msgs, blocked = gate_and_reserve(mb, 15, 100.0, 0.3, 15.0, yes=False)
    assert allowed == 0 and '已擋下' in blocked              # 餘額耗盡
