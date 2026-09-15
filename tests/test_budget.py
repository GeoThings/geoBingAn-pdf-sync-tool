"""budget：估算/門檻閘門/月累計/等級（預算守門）。"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.budget import estimate_cost, budget_gate, budget_level, monthly_gate, MonthlyBudget


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
