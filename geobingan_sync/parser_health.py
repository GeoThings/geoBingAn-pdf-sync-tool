"""後端解析引擎健康探測：上傳前先看「送進去會不會卡住」。

為什麼要有這支（2026-09-21）：上傳是免費的，但送進一個壞掉的解析佇列就會變成
pending／failed，而且**不會自己恢復**（後端 skip_reason=quota 不重試、午夜重置也不
放行），每一份都要之後人工 retry-parse。當天 OpenAI 組織帳戶餘額用光，4 份直接
failed、6 份撞應用層閘門，後端還把 billing 錯誤標成 invalid_json。與其事後清，
不如上傳前探一下：近 24 小時我方上傳的結果若出現 billing 失敗，或有 pending 卻
連續 6 小時沒有任何一份完成，今天就不上傳、記為擋下並告警。

純判斷在 `assess()`（可單元測試），網路在 `fetch_recent()`／`probe()`。
探測本身失敗（API 掛、token 壞）＝狀態未知 → **不放行**（fail-closed，與本專案
其他守門一致）；人工要繞過用 `--skip-parser-health`。
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

# 錯誤文字分類。後端 parse_failure_kind 不可信（billing 被標 invalid_json），只看原文。
BILLING_MARKERS = ('no credits', 'credits remaining', 'add credits', 'billing', 'insufficient_quota')
QUOTA_MARKERS = ('budget is exhausted', 'estimated-cost budget', 'daily budget')
DETERMINISTIC_MARKERS = ('輸出達長度上限', '分頁後重試', 'output length', 'max_output_tokens')

STALE_HOURS = 6          # pending/processing 超過這麼久、且期間零 completed → 視為停擺
WINDOW_HOURS = 24        # 只看近 24 小時我方上傳的結果


def classify_error(text: Optional[str]) -> Optional[str]:
    """回 'billing' | 'quota' | 'deterministic' | 'other' | None（無錯誤）。"""
    if not text:
        return None
    t = text.lower()
    if any(m in t for m in BILLING_MARKERS):
        return 'billing'
    if any(m in t for m in QUOTA_MARKERS):
        return 'quota'
    if any(m in text for m in DETERMINISTIC_MARKERS) or any(m in t for m in DETERMINISTIC_MARKERS):
        return 'deterministic'
    return 'other'


@dataclass
class Verdict:
    ok: bool
    reason: str
    stats: Dict[str, int] = field(default_factory=dict)


def _ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def assess(reports: List[dict], now: datetime, stale_hours: int = STALE_HOURS) -> Verdict:
    """純函式。reports 為含 metadata 的報告（detail 形狀），已限定為近期我方上傳。

    擋下的條件（任一）：
    1. 有 billing 失敗（帳戶沒餘額）——再送只會全部 failed。
    2. 停擺：有 pending/processing 且**沒有 quota/billing 錯誤**、年齡 ≥ stale_hours，
       而期間沒有任何一份 completed（parsed_at 在 stale_hours 內）——worker 死了。
    只撞應用層閘門（quota）**不擋**：那是額度用完的正常結果，午夜會重置，
    我方預算守門本來就會處理。
    """
    st = {'total': len(reports), 'completed': 0, 'pending': 0, 'processing': 0, 'failed': 0,
          'billing': 0, 'quota': 0, 'deterministic': 0, 'other_error': 0, 'stalled': 0,
          'completed_recent': 0}
    stale_cut = now - timedelta(hours=stale_hours)
    for r in reports:
        status = r.get('parse_status') or 'unknown'
        m = r.get('metadata') or {}
        kind = classify_error(m.get('parse_error') or m.get('skip_reason'))
        if status in st:
            st[status] += 1
        if kind == 'billing':
            st['billing'] += 1
        elif kind == 'quota':
            st['quota'] += 1
        elif kind == 'deterministic':
            st['deterministic'] += 1
        elif kind == 'other' and status == 'failed':
            st['other_error'] += 1
        if status == 'completed':
            pa = _ts(m.get('parsed_at'))
            if pa and pa >= stale_cut:
                st['completed_recent'] += 1
        if status in ('pending', 'processing') and kind not in ('quota', 'billing'):
            created = _ts(r.get('created_at'))
            if created and created <= stale_cut:
                st['stalled'] += 1

    if st['billing'] > 0:
        return Verdict(False, f'近 {WINDOW_HOURS}h 有 {st["billing"]} 份因 OpenAI 帳戶無餘額失敗'
                              f'（no credits）——加值前再送只會全部 failed', st)
    if st['stalled'] > 0 and st['completed_recent'] == 0:
        return Verdict(False, f'{st["stalled"]} 份 pending/processing 已超過 {stale_hours}h、'
                              f'期間零完成——疑 worker 停擺', st)
    return Verdict(True, f'解析引擎正常（近 {WINDOW_HOURS}h：completed {st["completed"]}、'
                         f'pending {st["pending"]}、quota 擋 {st["quota"]}）', st)


def fetch_recent(base: str, headers: dict, now: datetime, hours: int = WINDOW_HOURS,
                 detail_cap: int = 40, get: Callable = None) -> List[dict]:
    """近 hours 小時建立的報告；非 completed 的抓 detail 取 metadata，completed 只抓前 10 份
    的 detail（要 parsed_at）。detail 呼叫有上限避免拖慢每日上傳。"""
    import requests
    get = get or requests.get
    cut = now - timedelta(hours=hours)
    url = f'{base}/api/reports/construction-reports/'
    params = {'page_size': 100, 'ordering': '-created_at'}
    rows: List[dict] = []
    for _ in range(3):
        resp = get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        d = resp.json()
        params = None
        page = d.get('results') or []
        for r in page:
            t = _ts(r.get('created_at'))
            if t and t >= cut:
                rows.append(r)
        if not page or (_ts(page[-1].get('created_at')) or cut) < cut:
            break
        url = d.get('next')
        if not url:
            break
    out: List[dict] = []
    detail_used = 0
    completed_detail = 0
    for r in rows:
        status = r.get('parse_status')
        need_detail = status != 'completed' or completed_detail < 10
        if need_detail and detail_used < detail_cap:
            resp = get(f"{base}/api/reports/construction-reports/{r['id']}/", headers=headers, timeout=30)
            resp.raise_for_status()
            r = resp.json()
            detail_used += 1
            if status == 'completed':
                completed_detail += 1
        out.append(r)
    return out


def probe(now: Optional[datetime] = None, hours: int = WINDOW_HOURS) -> Verdict:
    """網路版：取 token、抓近期報告、assess。任何例外都回「未知＝不放行」。"""
    from geobingan_sync.steps.upload_pdfs import _get_valid_token
    from geobingan_sync.config import GEOBINGAN_BASE_URL
    now = now or datetime.now(timezone.utc)
    try:
        base = GEOBINGAN_BASE_URL.rstrip('/')
        headers = {'Authorization': f'Bearer {_get_valid_token()}'}
        reports = fetch_recent(base, headers, now, hours=hours)
    except Exception as e:  # noqa: BLE001 — 探測失敗＝狀態未知，不能當成健康
        return Verdict(False, f'探測失敗，解析引擎狀態未知：{type(e).__name__}: {str(e)[:80]}',
                       {'probe_error': 1})
    return assess(reports, now)


def hold_if_unhealthy(skip: bool = False, probe_fn: Callable[[], Verdict] = None) -> Verdict:
    """上傳前閘門：不健康就印原因並 SystemExit(4)。skip=True 只印不擋（人工繞過）。"""
    v = (probe_fn or probe)()
    tag = '✅' if v.ok else '🛑'
    print(f'  {tag} 解析引擎探測：{v.reason}')
    if not v.ok and not skip:
        print('\n🛑 解析引擎異常，今日暫停上傳（避免堆出不會自動恢復的 pending/failed）。'
              '人工確認後可加 --skip-parser-health 繞過。')
        raise SystemExit(4)
    if not v.ok and skip:
        print('  ⚠️ 已指定 --skip-parser-health，照常上傳')
    return v
