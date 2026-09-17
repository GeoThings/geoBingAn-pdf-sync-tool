"""解析預算守門（**日**模型）。

後端 PDF 解析成本受 OpenAI 專案花費上限硬性節制。2026-09-16 確認真實限制是
**每日 US$20**（約 66 份），不是先前假設的月上限——月模型會在還有日額度時
無謂擋下上傳，日模型才對得上後端的實際節流。

**額度是共用的**：夜間上傳與手動 retry-parse 吃同一份日額度。2026-09-14 一次
上傳 178 份打爆額度、116 份卡死，就是沒有這道守門。retry 也必須計入，否則
重推吃掉額度而守門毫不知情，當晚上傳會再爆一次。

這裡把那道看不見的外部硬上限，變成我們這邊看得見、可控制的軟上限：

- estimate_cost / budget_gate：上傳前印估算，單次超過門檻需 --yes（夜間排程配合
  MAX_UPLOADS 永遠不會超過門檻，只擋人為的大批次）。
- DailyBudget：state/upload_budget.json 記當日消耗份數（上傳＋重推）與估算成本
  （state/*.json 已 gitignore），供 health_check 檢查並在 70%/90% 告警。
"""
import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

WARN_RATIO = 0.7
ERROR_RATIO = 0.9


def day_key(now: Optional[datetime] = None) -> str:
    """回傳帳本的「日」鍵，**以 UTC 計**。

    後端的日額度由 provider（OpenAI）依 UTC 重置；若我們用主機本地時間（台北
    UTC+8）算日界，帳本會在台北午夜＝UTC 16:00 就歸零，比後端**提早 8 小時**
    重新放行整份額度（review 指出的風險）。用 UTC 對齊；即使後端其實依本地時間
    重置，UTC 也只會讓我們晚一點才重置＝偏保守，不會超額。

    傳入 naive datetime 時視為「已是預期基準」（測試注入用），不再轉換。
    """
    if now is None:
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc).strftime('%Y-%m-%d')
    return now.strftime('%Y-%m-%d')


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


def daily_gate(n_reports: int, day_est_usd: float, daily_budget: float,
               cost_per_report: float, yes: bool) -> Tuple[int, str]:
    """日上限放行條件：投影成本＝今日已用（上傳＋重推）＋本次估算。

    回傳 (允許份數, 訊息)：
    - 投影 ≤ 日上限：全數放行。
    - 超過且 yes=False（夜間/一般）：裁切到剩餘預算可容納的份數（0 則擋下）。
    - 超過且 yes=True（人工明確覆寫）：全數放行但標警告。
    daily_budget ≤ 0 視為未設上限。
    """
    n = max(0, n_reports)
    if daily_budget <= 0 or n == 0:
        return n, ''
    projected = round(day_est_usd + estimate_cost(n, cost_per_report), 2)
    if projected <= daily_budget:
        return n, f'投影今日成本 US${projected:.2f} ≤ 日上限 US${daily_budget:.0f}'
    remaining = max(0.0, daily_budget - day_est_usd)
    allowed = int(remaining // cost_per_report) if cost_per_report > 0 else 0
    if yes:
        return n, (f'⚠️ 投影今日成本 US${projected:.2f} 超過日上限 US${daily_budget:.0f}，'
                   f'已以 --yes 明確覆寫、全數 {n} 份執行')
    if allowed <= 0:
        return 0, (f'今日已用 US${day_est_usd:.2f}、日上限 US${daily_budget:.0f}，剩餘額度不足 1 份；'
                   f'已擋下（明日額度重置後再跑，或確認後加 --yes）')
    return allowed, (f'投影今日成本 US${projected:.2f} 超過日上限 US${daily_budget:.0f}，'
                     f'自動裁切為今日剩餘可容納的 {allowed} 份（原 {n} 份；加 --yes 可覆寫）')


def budget_level(est_usd: float, daily_budget: float) -> Tuple[str, float]:
    """依已用比例回 ('ok'|'warning'|'error', ratio)。"""
    if daily_budget <= 0:
        return 'ok', 0.0
    ratio = est_usd / daily_budget
    if ratio >= ERROR_RATIO:
        return 'error', ratio
    if ratio >= WARN_RATIO:
        return 'warning', ratio
    return 'ok', ratio


class DailyBudget:
    """state/upload_budget.json：{"day": "2026-09-17", "uploaded": N, "retried": M,
    "units": N+M, "est_usd": X}。**跨日自動歸零**。

    units＝當日送進後端解析的總份數（上傳＋手動 retry-parse）。兩者吃同一份日額度，
    所以必須合計；只算上傳會讓重推的消耗對守門隱形。

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
        day = day_key(now)
        data = self._read_raw()
        if data.get('day') != day:
            # 跨日（或舊的月格式檔）一律重置——日額度每天重來
            data = {'day': day, 'uploaded': 0, 'retried': 0, 'units': 0, 'est_usd': 0.0}
        data.setdefault('uploaded', 0)
        data.setdefault('retried', 0)
        data.setdefault('units', int(data.get('uploaded', 0)) + int(data.get('retried', 0)))
        return data

    def _write(self, data: dict, now: datetime) -> dict:
        data['uploaded'] = max(0, int(data.get('uploaded', 0)))
        data['retried'] = max(0, int(data.get('retried', 0)))
        data['units'] = data['uploaded'] + data['retried']
        data['est_usd'] = round(data['units'] * self.cost_per_report, 2)
        data['updated_at'] = now.isoformat()
        tmp = self.path.with_name(f'{self.path.name}.{os.getpid()}.tmp')   # 含 PID，不共用暫存檔
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        return data

    def load(self, now: Optional[datetime] = None) -> dict:
        """唯讀取用（顯示用）。"""
        return self._read(now or datetime.now())

    def add(self, n: int, now: Optional[datetime] = None, kind: str = 'uploaded') -> dict:
        """鎖內累加（n 可為負，下限 0）。kind＝'uploaded' 或 'retried'。"""
        assert kind in ('uploaded', 'retried')
        now = now or datetime.now()
        with self._locked():
            data = self._read(now)
            data[kind] = int(data.get(kind, 0)) + int(n)
            return self._write(data, now)

    def record_retry(self, n: int, now: Optional[datetime] = None) -> dict:
        """事後補記 retry 消耗（**不預留**）。

        ⚠️ 只用於補記工具外已發生的消耗。正常重推請走 steps.retry_parse，它會先
        reserve 再送出——事後記帳擋不住「retry 已送出但尚未記帳時，夜間上傳讀到
        用量為 0 而超額」的競態（review P1）。
        """
        return self.add(n, now=now, kind='retried')

    def reserve_retry(self, n_requested: int, daily_budget: float, cost_per_report: float,
                      yes: bool, now: Optional[datetime] = None) -> Tuple[int, str, dict]:
        """重推專用的原子預留：與上傳走同一把鎖、同一份日額度，先佔再送。"""
        now = now or datetime.now()
        with self._locked():
            data = self._read(now)
            allowed, msg = daily_gate(n_requested, float(data.get('est_usd', 0.0)),
                                      daily_budget, cost_per_report, yes)
            if allowed > 0:
                data['retried'] = int(data.get('retried', 0)) + allowed
                data = self._write(data, now)
            return allowed, msg, data

    def reserve(self, n_requested: int, daily_budget: float, cost_per_report: float,
                yes: bool, now: Optional[datetime] = None) -> Tuple[int, str, dict]:
        """鎖內原子預留：讀今日餘額 → daily_gate → 立刻計入可放行份數。回 (允許份數, 訊息, 狀態)。"""
        now = now or datetime.now()
        with self._locked():
            data = self._read(now)
            allowed, msg = daily_gate(n_requested, float(data.get('est_usd', 0.0)),
                                      daily_budget, cost_per_report, yes)
            if allowed > 0:
                data['uploaded'] = int(data.get('uploaded', 0)) + allowed
                data = self._write(data, now)
            return allowed, msg, data

    def release(self, unused: int, now: Optional[datetime] = None,
                day: Optional[str] = None, kind: str = 'uploaded') -> dict:
        """退還未用到的預留，綁定預留所屬日期（跨日同理於原跨月保護）。

        鎖內先讀檔案實際儲存的日期：與 day 相同才扣減；帳本已切到新的一天則 no-op
        （不重建昨日、不動今日的額度）。day=None 時退回舊行為（扣當日）。
        """
        now = now or datetime.now()
        if unused <= 0:
            return self.load(now)
        with self._locked():
            if day is not None:
                stored = self._read_raw().get('day')
                if stored != day:
                    return self._read(now)          # 昨日預留跨日退還 → 不動任何數字
            data = self._read(now)
            data[kind] = int(data.get(kind, 0)) - int(unused)
            return self._write(data, now)

    def set_uploaded(self, n: int, now: Optional[datetime] = None) -> dict:
        """明確設定今日上傳份數（重建/校正用：python -m geobingan_sync.budget --set N）。"""
        now = now or datetime.now()
        with self._locked():
            data = self._read(now)
            data['uploaded'] = int(n)
            return self._write(data, now)


class ReservationLedger:
    """把預留當成「已消耗」，只對確定零成本的項目退還（review P1：成功後中斷不可退）。

    - begin_item()：在 POST 前一刻呼叫，通過即視為已嘗試（已消耗）；若已跨月回 False，批次須停止。
    - settle(result)：只有 error ∈ REFUNDABLE（後端明確拒絕＝沒建報告）才立即退 1 份；
      下載失敗未進入已嘗試、由 close() 退還；成功或結果不明（逾時/未知例外）都保留。
    - close()：只退還「預留 − 已嘗試」＝從未嘗試的份數。任何在 settle 之前的中斷都不會
      退還該項目（方向安全，最壞多算）。
    """
    # 只有「後端明確拒絕」需要即時退款：下載失敗的項目從未呼叫 begin_item（未計入已嘗試），
    # close() 會把它當作未嘗試退還，若這裡再退會重複。
    REFUNDABLE = frozenset({'rejected'})

    def __init__(self, mb: 'DailyBudget', reserved: int, day: Optional[str] = None,
                 clock=None, kind: str = 'uploaded'):
        self.mb = mb
        self.reserved = max(0, int(reserved))
        self.day = day                          # 預留所屬日期；退還只作用於同日帳本
        self.kind = kind                        # 'uploaded' 或 'retried'——退還要記回同一欄
        self.clock = clock or datetime.now      # 可注入時鐘（測試模擬下載期間跨月）
        self.attempted = 0
        self.refunded = 0

    def begin_item(self, now: Optional[datetime] = None) -> bool:
        """在「下載完成、第一個 HTTP POST 之前」呼叫（作為 process_single_pdf 的 before_upload）。
        回 True 表示可送出並已計入已嘗試；回 False 表示已跨日，本批次應停止
        （跨日後送出的成本應占用新一天的額度，不可再用昨日預留；該項目不寫 error/歷史，
        剩餘項目下次執行會在新的一天重新預留）。"""
        if self.day is not None:
            now = now or self.clock()
            if day_key(now) != self.day:
                return False
        self.attempted += 1
        return True

    def settle(self, result: dict) -> bool:
        if result.get('success'):
            return False
        if result.get('error') in self.REFUNDABLE:
            self.mb.release(1, day=self.day, kind=self.kind)
            self.refunded += 1
            return True
        return False

    def close(self) -> dict:
        unused = self.reserved - self.attempted
        return self.mb.release(unused, day=self.day, kind=self.kind) if unused > 0 else self.mb.load()


def gate_and_reserve(mb: 'DailyBudget', n_requested: int, daily_budget: float,
                     cost_per_report: float, confirm_threshold: float, yes: bool,
                     kind: str = 'uploaded'):
    """單次門檻 → 原子預留，順序固定（review TOCTOU）。

    單次門檻對「原始請求量」檢查而非對預覽份數：預留結果永遠 ≤ 原始請求量，
    所以另一程序在中途退還額度或跨日，都不可能讓放行份數超過已確認的量。
    門檻擋下時尚未預留，不需退還。回 (允許份數, 訊息列表, 擋下原因或 None, 預留所屬日期)。
    """
    msgs = []
    ok, gate_msg = budget_gate(n_requested, cost_per_report, confirm_threshold, yes)
    msgs.append(gate_msg)
    if n_requested > 0 and not ok:
        return 0, msgs, gate_msg, None
    reserve = mb.reserve_retry if kind == 'retried' else mb.reserve
    allowed, day_msg, data = reserve(n_requested, daily_budget, cost_per_report, yes)
    if day_msg:
        msgs.append(day_msg)
    if n_requested > 0 and allowed <= 0:
        return 0, msgs, day_msg, data.get('day')
    return allowed, msgs, None, data.get('day')


def _cli():
    import argparse
    from geobingan_sync.config import COST_PER_REPORT_USD, DAILY_BUDGET_USD
    ap = argparse.ArgumentParser(description='今日解析預算計數（state/upload_budget.json 為本機狀態、不入版控）')
    ap.add_argument('--show', action='store_true', help='顯示今日累計')
    ap.add_argument('--set', type=int, metavar='N', help='明確設定今日上傳份數（重建/校正）')
    ap.add_argument('--reconcile-retry', type=int, metavar='N',
                    help='⚠️ 僅供補記「已在工具外發生」的 retry 消耗（不預留、無法防超額）。'
                         '正常重推請用 python -m geobingan_sync.steps.retry_parse，它會先預留再送出')
    a = ap.parse_args()
    mb = DailyBudget(cost_per_report=COST_PER_REPORT_USD)
    if a.set is not None:
        d = mb.set_uploaded(a.set)
    elif a.reconcile_retry is not None:
        d = mb.record_retry(a.reconcile_retry)
    else:
        d = mb.load()
    level, ratio = budget_level(float(d.get('est_usd', 0)), DAILY_BUDGET_USD)
    print(f"今日({d['day']}) 上傳 {d['uploaded']} ＋重推 {d['retried']} ＝ {d['units']} 份 "
          f"≈ US${float(d.get('est_usd', 0)):.2f} / 日上限 US${DAILY_BUDGET_USD:.0f}（{ratio:.0%}，{level}）")


if __name__ == '__main__':
    _cli()
