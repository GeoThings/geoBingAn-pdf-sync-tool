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
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

STATE_FILE = './state/parser_health.json'

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


def load_state(path: str = STATE_FILE) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (ValueError, OSError):
        return {}


def save_state(state: dict, path: str = STATE_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp.{os.getpid()}'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def count_recovered_after(reports: List[dict], bad_at: Optional[datetime]) -> int:
    """算「事故之後」完成的份數＝真正的恢復證據。

    bad_at 為 None（舊狀態檔沒記時間）時保守回 0：寧可多 held 一輪，由 canary 解除，
    也不要用來路不明的完成紀錄開閘。
    """
    if bad_at is None:
        return 0
    n = 0
    for r in reports:
        if (r.get('parse_status') or '') != 'completed':
            continue
        pa = _ts(((r.get('metadata') or {}).get('parsed_at')))
        if pa and pa > bad_at:
            n += 1
    return n


def evaluate(reports: List[dict], now: datetime, prev: Optional[dict] = None,
             stale_hours: int = STALE_HOURS) -> Tuple[Verdict, dict]:
    """把「這次觀測」與「上次已知狀態」合起來判斷，回 (Verdict, 新狀態)。

    為什麼需要狀態（2026-09-22 實例）：`assess()` 只看視窗內的報告，**空視窗會回
    健康**。前一天 11:00 上傳的 billing 失敗在隔天 12:00 已滑出 24h 視窗，於是探測
    印出「解析引擎正常（completed 0、pending 0、quota 擋 0）」並放行 15 份——帳戶
    其實還沒加值，只是我們剛好看不到證據。**沒有證據不等於健康**。

    規則：
    - 觀測到壞（billing／停擺）→ 記為 held，寫入原因與時間。
    - 觀測到**正面證據**（視窗內有 completed）→ 清除 held。
    - 觀測不到任何東西（空視窗／只有剛上傳的 pending）→ **維持上次狀態**。
      先前 held 就繼續 held，直到看見恢復證據為止。

    卡死的出口：`drain_stuck` 在 held-for-billing 時會送 1 份 canary 測試（見該檔），
    或人工 `--skip-parser-health`。
    """
    prev = prev or {}
    v = assess(reports, now, stale_hours=stale_hours)
    st = dict(prev)
    if not v.ok:
        st.update({'status': 'held', 'reason': v.reason, 'since': st.get('since') or now.isoformat(),
                   'kind': 'billing' if v.stats.get('billing') else 'stalled',
                   'last_bad': now.isoformat()})
        return v, st

    if prev.get('status') == 'held':
        # 解除只認「事故之後」的完成（review P1）。視窗是滑動的 24h，事故當天稍早
        # 成功的那些也還在視窗內——拿它們當恢復證據等於用事故前的資料開閘。
        bad_at = _ts(prev.get('last_bad')) or _ts(prev.get('since'))
        n = count_recovered_after(reports, bad_at)
        if n > 0:
            when = f'{bad_at:%m-%d %H:%M}' if bad_at else '事故'
            return (Verdict(True, f'解析引擎已恢復（{when} 之後有 {n} 份完成；'
                                  f'先前 held 原因：{prev.get("reason", "")[:40]}）', v.stats),
                    {'status': 'ok', 'last_ok': now.isoformat()})
        held = Verdict(False, f'仍 held：視窗內 {v.stats.get("completed", 0)} 份完成都在事故之前，'
                              f'沒有恢復證據（{prev.get("reason", "")[:50]}）',
                       dict(v.stats, recovered_after_bad=0))
        return held, st

    if v.stats.get('completed', 0) > 0:
        return v, {'status': 'ok', 'last_ok': now.isoformat()}

    # 沒有前科、也沒有完成：沿用先前（ok）狀態，不因「看不到」而誤擋
    return Verdict(True, v.reason + '（視窗內無完成紀錄，沿用先前狀態）', v.stats), st


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
        if r.get('_no_detail'):
            st['no_detail'] = st.get('no_detail', 0) + 1
        elif status in ('pending', 'processing') and kind not in ('quota', 'billing'):
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
                 detail_cap: int = 150, get: Callable = None,
                 our_names: Optional[set] = None) -> List[dict]:
    """近 hours 小時建立、**我方上傳**的報告（review P2：文件宣稱我方，程式就要真的限定，
    否則別人上傳的病態檔會把我們的上傳擋住）。our_names＝正規化檔名集合（見 drain_stuck.norm_name），
    None 表示不過濾（測試用）。

    非 completed 的**全部**抓 detail 取 metadata——只有 detail 才看得到 parse_error，
    沒 detail 的 pending 分不出「撞 quota」和「真的停擺」（review P2：原 detail_cap=40
    超過後 quota pending 會被誤判成 worker 停擺）。每日量 ≤ 70，hard cap 150 只是保險；
    超過的標 `_no_detail=True`，assess 不把它們算進 stalled。completed 只抓前 10 份
    的 detail（要 parsed_at）。"""
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
    if our_names is not None:
        from geobingan_sync.steps.drain_stuck import norm_name
        rows = [r for r in rows if norm_name(r.get('file_name') or '') in our_names]
    out: List[dict] = []
    detail_used = 0
    completed_detail = 0
    for r in rows:
        status = r.get('parse_status')
        need_detail = status != 'completed' or completed_detail < 10
        if need_detail:
            if detail_used < detail_cap:
                resp = get(f"{base}/api/reports/construction-reports/{r['id']}/", headers=headers, timeout=30)
                resp.raise_for_status()
                r = resp.json()
                detail_used += 1
                if status == 'completed':
                    completed_detail += 1
            else:
                r = dict(r, _no_detail=True)      # 看不到 parse_error → 不可歸類，不算 stalled
        out.append(r)
    return out


def fetch_by_ids(base: str, headers: dict, ids: List[str], get: Callable = None) -> List[dict]:
    """依 id 直接抓 detail。canary 重推的是**幾天前建立**的報告，24h 的 created_at 視窗
    抓不到它（review P1：canary 成功也解不開 held）——所以按 id 補抓。"""
    import requests
    get = get or requests.get
    out = []
    for rid in ids:
        try:
            resp = get(f'{base}/api/reports/construction-reports/{rid}/', headers=headers, timeout=30)
            resp.raise_for_status()
            out.append(resp.json())
        except Exception:          # noqa: BLE001 — 單筆抓不到就跳過，不影響其餘判斷
            continue
    return out


def probe(now: Optional[datetime] = None, hours: int = WINDOW_HOURS,
          state_path: str = STATE_FILE) -> Verdict:
    """網路版：取 token、抓近期報告、evaluate（含持久化狀態）。

    任何例外都回「未知＝不放行」，且**不寫狀態**（探測沒成功就沒有新資訊，
    不可因此清掉先前的 held）。
    """
    from geobingan_sync.steps.upload_pdfs import _get_valid_token
    from geobingan_sync.config import GEOBINGAN_BASE_URL
    from geobingan_sync.steps.drain_stuck import load_our_names
    now = now or datetime.now(timezone.utc)
    try:
        base = GEOBINGAN_BASE_URL.rstrip('/')
        headers = {'Authorization': f'Bearer {_get_valid_token()}'}
        prev = load_state(state_path)
        reports = fetch_recent(base, headers, now, hours=hours, our_names=load_our_names())
        seen = {r.get('id') for r in reports}
        extra = [r for r in fetch_by_ids(base, headers, prev.get('canary_ids') or [])
                 if r.get('id') not in seen]
        reports = reports + extra
    except Exception as e:  # noqa: BLE001 — 探測失敗＝狀態未知，不能當成健康
        return Verdict(False, f'探測失敗，解析引擎狀態未知：{type(e).__name__}: {str(e)[:80]}',
                       {'probe_error': 1})
    v, st = evaluate(reports, now, prev)
    save_state(st, state_path)
    return v


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
