"""解析預算守門。

後端 PDF 解析成本受 OpenAI 專案的花費上限硬性節制（2026-09-14 一次上傳 178 份
就把當月上限打爆、後續 116 份全卡 pending）。這裡把那道看不見的外部硬上限，
變成我們這邊看得見、可控制的軟上限：

- estimate_cost / budget_gate：上傳前印估算，單次超過門檻需 --yes（夜間排程配合
  MAX_UPLOADS 永遠不會超過門檻，只擋人為的大批次）。
- MonthlyBudget：state/upload_budget.json 記當月已傳份數與估算成本（state/*.json 已
  gitignore），供 health_check 每日檢查並在 70%/90% 告警。
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

WARN_RATIO = 0.7
ERROR_RATIO = 0.9


def estimate_cost(n_reports: int, cost_per_report: float) -> float:
    return round(max(0, n_reports) * cost_per_report, 2)


def budget_gate(n_reports: int, cost_per_report: float, confirm_threshold: float,
                yes: bool) -> Tuple[bool, str]:
    """回傳 (可繼續, 訊息)。估算超過門檻且未帶 --yes 就擋下。"""
    est = estimate_cost(n_reports, cost_per_report)
    if est > confirm_threshold and not yes:
        return False, (f'估算解析成本 US${est:.2f}（{n_reports} 份 × US${cost_per_report}）'
                       f'超過確認門檻 US${confirm_threshold:.0f}；請縮小 --catchup-days / 降低 MAX_UPLOADS 分批，'
                       f'或確認預算餘裕後加 --yes 執行')
    return True, f'估算解析成本 ≈ US${est:.2f}（{n_reports} 份 × US${cost_per_report}）'


def monthly_gate(n_reports: int, month_est_usd: float, monthly_budget: float,
                 cost_per_report: float, yes: bool) -> Tuple[int, str]:
    """月上限放行條件（review P1）：投影成本＝本月已用＋本次估算。

    回傳 (允許份數, 訊息)：
    - 投影 ≤ 月上限：全數放行。
    - 超過且 yes=False（夜間/一般）：裁切到剩餘預算可容納的份數（0 則擋下）。
    - 超過且 yes=True（人工明確覆寫）：全數放行但標警告。
    monthly_budget ≤ 0 視為未設上限。
    """
    n = max(0, n_reports)
    if monthly_budget <= 0 or n == 0:
        return n, ''
    projected = round(month_est_usd + estimate_cost(n, cost_per_report), 2)
    if projected <= monthly_budget:
        return n, f'投影本月成本 US${projected:.2f} ≤ 上限 US${monthly_budget:.0f}'
    remaining = max(0.0, monthly_budget - month_est_usd)
    allowed = int(remaining // cost_per_report) if cost_per_report > 0 else 0
    if yes:
        return n, (f'⚠️ 投影本月成本 US${projected:.2f} 超過上限 US${monthly_budget:.0f}，'
                   f'已以 --yes 明確覆寫、全數 {n} 份執行')
    if allowed <= 0:
        return 0, (f'本月已用 US${month_est_usd:.2f}、上限 US${monthly_budget:.0f}，剩餘預算不足 1 份；'
                   f'已擋下（請與後端確認預算後加 --yes，或等下月）')
    return allowed, (f'投影本月成本 US${projected:.2f} 超過上限 US${monthly_budget:.0f}，'
                     f'自動裁切為剩餘預算可容納的 {allowed} 份（原 {n} 份；加 --yes 可覆寫）')


def budget_level(est_usd: float, monthly_budget: float) -> Tuple[str, float]:
    """依已用比例回 ('ok'|'warning'|'error', ratio)。"""
    if monthly_budget <= 0:
        return 'ok', 0.0
    ratio = est_usd / monthly_budget
    if ratio >= ERROR_RATIO:
        return 'error', ratio
    if ratio >= WARN_RATIO:
        return 'warning', ratio
    return 'ok', ratio


class MonthlyBudget:
    """state/upload_budget.json：{"month": "2026-09", "uploaded": N, "est_usd": X}。跨月自動歸零。"""

    def __init__(self, path: Optional[Path] = None, cost_per_report: float = 0.3):
        if path is None:
            path = Path(__file__).resolve().parent.parent / 'state' / 'upload_budget.json'
        self.path = Path(path)
        self.cost_per_report = cost_per_report

    def load(self, now: Optional[datetime] = None) -> dict:
        now = now or datetime.now()
        month = now.strftime('%Y-%m')
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        if data.get('month') != month:
            data = {'month': month, 'uploaded': 0, 'est_usd': 0.0}
        return data

    def add(self, n_uploaded: int, now: Optional[datetime] = None) -> dict:
        data = self.load(now)
        data['uploaded'] = int(data.get('uploaded', 0)) + max(0, n_uploaded)
        data['est_usd'] = round(data['uploaded'] * self.cost_per_report, 2)
        data['updated_at'] = (now or datetime.now()).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)
        return data
