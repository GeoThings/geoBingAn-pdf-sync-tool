"""解析預算守門。

後端 PDF 解析成本受 OpenAI 專案的花費上限硬性節制（2026-09-14 一次上傳 178 份
就把當月上限打爆、後續 116 份全卡 pending）。這裡把那道看不見的外部硬上限，
變成我們這邊看得見、可控制的軟上限：

- estimate_cost / budget_gate：上傳前印估算，單次超過門檻需 --yes（夜間排程配合
  MAX_UPLOADS 永遠不會超過門檻，只擋人為的大批次）。
- MonthlyBudget：state/upload_budget.json 記當月已傳份數與估算成本（state/*.json 已
  gitignore），供 health_check 每日檢查並在 70%/90% 告警。
"""
import fcntl
import json
import os
from contextlib import contextmanager
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
    """state/upload_budget.json：{"month": "2026-09", "uploaded": N, "est_usd": X}。跨月自動歸零。

    併發與中斷安全（review P1×2）：
    - 所有讀改寫都在跨程序鎖 fcntl.flock（state/upload_budget.lock）內完成，排程與人工重疊
      時不會讀到同一筆餘額各自放行；暫存檔名含 PID，不共用 .tmp。
    - reserve() 在鎖內「讀餘額 → 算可放行份數 → 立刻計入」（先預留）；批次結束用 release()
      退還「預留 − 實際成功」。程序被中斷時已成功的份數仍在帳上（最壞只會多算，方向安全，
      不會低估而突破後端硬上限）。
    """

    def __init__(self, path: Optional[Path] = None, cost_per_report: float = 0.3):
        if path is None:
            path = Path(__file__).resolve().parent.parent / 'state' / 'upload_budget.json'
        self.path = Path(path)
        self.lock_path = self.path.with_suffix('.lock')
        self.cost_per_report = cost_per_report

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, 'a+') as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _read_raw(self) -> dict:
        """讀檔案實際內容（不依現在時間換月），供跨月判斷用。"""
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _read(self, now: datetime) -> dict:
        month = now.strftime('%Y-%m')
        data = self._read_raw()
        if data.get('month') != month:
            data = {'month': month, 'uploaded': 0, 'est_usd': 0.0}
        return data

    def _write(self, data: dict, now: datetime) -> dict:
        data['uploaded'] = max(0, int(data.get('uploaded', 0)))
        data['est_usd'] = round(data['uploaded'] * self.cost_per_report, 2)
        data['updated_at'] = now.isoformat()
        tmp = self.path.with_name(f'{self.path.name}.{os.getpid()}.tmp')   # 含 PID，不共用暫存檔
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        return data

    def load(self, now: Optional[datetime] = None) -> dict:
        """唯讀取用（顯示用）。"""
        return self._read(now or datetime.now())

    def add(self, n: int, now: Optional[datetime] = None) -> dict:
        """鎖內累加（n 可為負，下限 0）。"""
        now = now or datetime.now()
        with self._locked():
            data = self._read(now)
            data['uploaded'] = int(data.get('uploaded', 0)) + int(n)
            return self._write(data, now)

    def reserve(self, n_requested: int, monthly_budget: float, cost_per_report: float,
                yes: bool, now: Optional[datetime] = None) -> Tuple[int, str, dict]:
        """鎖內原子預留：讀餘額 → monthly_gate → 立刻計入可放行份數。回 (允許份數, 訊息, 狀態)。"""
        now = now or datetime.now()
        with self._locked():
            data = self._read(now)
            allowed, msg = monthly_gate(n_requested, float(data.get('est_usd', 0.0)),
                                        monthly_budget, cost_per_report, yes)
            if allowed > 0:
                data['uploaded'] = int(data.get('uploaded', 0)) + allowed
                data = self._write(data, now)
            return allowed, msg, data

    def release(self, unused: int, now: Optional[datetime] = None,
                month: Optional[str] = None) -> dict:
        """退還未用到的預留，綁定預留所屬月份（review P1 跨月）。

        鎖內先讀檔案實際儲存的月份：與 month 相同才扣減；帳本已切到新月份則 no-op
        （不重建舊月、不動新月的額度）。month=None 時退回舊行為（扣當月）。
        """
        now = now or datetime.now()
        if unused <= 0:
            return self.load(now)
        with self._locked():
            if month is not None:
                stored = self._read_raw().get('month')
                if stored != month:
                    return self._read(now)          # 舊月預留跨月退還 → 不動任何數字
            data = self._read(now)
            data['uploaded'] = int(data.get('uploaded', 0)) - int(unused)
            return self._write(data, now)

    def set_uploaded(self, n: int, now: Optional[datetime] = None) -> dict:
        """明確設定本月份數（換機/重建時的初始化：python -m geobingan_sync.budget --set N）。"""
        now = now or datetime.now()
        with self._locked():
            data = self._read(now)
            data['uploaded'] = int(n)
            return self._write(data, now)


class ReservationLedger:
    """把預留當成「已消耗」，只對確定零成本的項目退還（review P1：成功後中斷不可退）。

    - begin_item()：項目一進入處理即視為已嘗試（已消耗）；若已跨月回 False，批次須停止。
    - settle(result)：只有 error ∈ REFUNDABLE（下載失敗＝沒打 API；後端明確拒絕＝沒建報告）
      才立即退 1 份；成功或結果不明（逾時/未知例外）都保留。
    - close()：只退還「預留 − 已嘗試」＝從未嘗試的份數。任何在 settle 之前的中斷都不會
      退還該項目（方向安全，最壞多算）。
    """
    REFUNDABLE = frozenset({'download_failed', 'rejected'})

    def __init__(self, mb: 'MonthlyBudget', reserved: int, month: Optional[str] = None):
        self.mb = mb
        self.reserved = max(0, int(reserved))
        self.month = month                      # 預留所屬月份；退還只作用於同月帳本
        self.attempted = 0
        self.refunded = 0

    def begin_item(self, now: Optional[datetime] = None) -> bool:
        """項目送出前呼叫。回 True 表示可送出並已計入已嘗試；回 False 表示已跨月，
        本批次應停止（review P1：跨月後送出的成本應占用新月額度，不可再用舊月預留；
        剩餘項目不在去重歷史裡，下次執行會在新月份重新預留）。"""
        if self.month is not None:
            now = now or datetime.now()
            if now.strftime('%Y-%m') != self.month:
                return False
        self.attempted += 1
        return True

    def settle(self, result: dict) -> bool:
        if result.get('success'):
            return False
        if result.get('error') in self.REFUNDABLE:
            self.mb.release(1, month=self.month)
            self.refunded += 1
            return True
        return False

    def close(self) -> dict:
        unused = self.reserved - self.attempted
        return self.mb.release(unused, month=self.month) if unused > 0 else self.mb.load()


def gate_and_reserve(mb: 'MonthlyBudget', n_requested: int, monthly_budget: float,
                     cost_per_report: float, confirm_threshold: float, yes: bool):
    """單次門檻 → 原子預留，順序固定（review TOCTOU）。

    單次門檻對「原始請求量」檢查而非對預覽份數：預留結果永遠 ≤ 原始請求量，
    所以另一程序在中途退還額度或跨月，都不可能讓放行份數超過已確認的量。
    門檻擋下時尚未預留，不需退還。回 (允許份數, 訊息列表, 擋下原因或 None, 預留所屬月份)。
    """
    msgs = []
    ok, gate_msg = budget_gate(n_requested, cost_per_report, confirm_threshold, yes)
    msgs.append(gate_msg)
    if n_requested > 0 and not ok:
        return 0, msgs, gate_msg, None
    allowed, month_msg, data = mb.reserve(n_requested, monthly_budget, cost_per_report, yes)
    if month_msg:
        msgs.append(month_msg)
    if n_requested > 0 and allowed <= 0:
        return 0, msgs, month_msg, data.get('month')
    return allowed, msgs, None, data.get('month')


def _cli():
    import argparse
    from geobingan_sync.config import COST_PER_REPORT_USD, MONTHLY_BUDGET_USD
    ap = argparse.ArgumentParser(description='本月解析預算計數（state/upload_budget.json 為本機狀態、不入版控）')
    ap.add_argument('--show', action='store_true', help='顯示本月累計')
    ap.add_argument('--set', type=int, metavar='N', help='明確設定本月已傳份數（換機/重建初始化）')
    a = ap.parse_args()
    mb = MonthlyBudget(cost_per_report=COST_PER_REPORT_USD)
    d = mb.set_uploaded(a.set) if a.set is not None else mb.load()
    level, ratio = budget_level(float(d.get('est_usd', 0)), MONTHLY_BUDGET_USD)
    print(f"本月({d['month']})已傳 {d['uploaded']} 份 ≈ US${float(d.get('est_usd', 0)):.2f} / 上限 US${MONTHLY_BUDGET_USD:.0f}（{ratio:.0%}，{level}）")


if __name__ == '__main__':
    _cli()
