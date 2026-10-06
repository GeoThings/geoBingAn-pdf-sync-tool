"""同步錯誤的落檔、連續失敗計數與保留窗。

為什麼需要這支：`state['errors']` 原本的項目只有 `{permit, error}`，**沒有時間戳**。
實測正式環境累積 2,067 筆，今天的 10 筆和一年前那筆 `Invalid URL ID` 長得完全一樣
——資料存在，但答不出「今天有沒有出錯」。所以不是「有資料沒人看」，是「資料無法
被查詢」；加告警之前得先讓它答得出問題。

門檻常數放這裡、由 health_check import，不讓兩邊各寫一份（同
feedback_no_alerting_vacuum_between_checks：不可複製對方的門檻）。
"""
from datetime import datetime, timedelta

#: 同一案連續失敗幾輪才算「有定論」。一次失敗是無定論的——實測單次網路抖動
#: 會同時打中執行緒池的 5–10 案，那不是個案壞了。
ERROR_STREAK_ALERT = 3

#: 單輪錯誤數佔**實際走訪案數**的比例，超過視為系統性故障（憑證失效、網路斷）。
#: 校準依據：2026-10-05 一次抖動 10/452 = 2.2% 不該告警；執行緒池 5 條，
#: 抖動規模 5–10 筆是常態，所以門檻要和那個量級拉開距離。
RUN_ERROR_RATE_ALERT = 0.05

#: errors 陣列的保留窗。超出的丟掉，但**計數器不受影響**（見 finalize_run）。
KEEP_ERROR_DAYS = 30
MAX_ERRORS = 500

#: 既有無時間戳項目搬去這裡，不直接刪——「某案從以前就一直失敗」還埋在裡面，
#: 而且 folder_deaths 的前例是 append-only 歷史有價值。只搬一次。
LEGACY_KEY = 'errors_legacy'


def record_error(state: dict, permit: str, error: str,
                 now: datetime = None, run: str = None) -> dict:
    """記一筆錯誤，**一定帶時間戳**。回傳寫進去的那一筆。

    三個呼叫點（bundle 寫入失敗／adapter 取檔失敗／sync_permit 例外）都走這裡，
    時間戳規則才只有一份；留在呼叫點自律就會漏（adapter 那兩處本來就同時
    標 processed 又記錯誤）。
    """
    now = now or datetime.now()
    entry = {'permit': permit, 'error': str(error), 'at': now.isoformat()}
    if run:
        entry['run'] = run
    state.setdefault('errors', []).append(entry)
    return entry


#: 搬遷摘要裡保留的樣本筆數與錯誤分類上限
LEGACY_SAMPLE = 20
LEGACY_TOP_ERRORS = 20
_ERROR_KEY_LEN = 60


def migrate_legacy(state: dict) -> int:
    """把無時間戳的舊項目一次性壓成摘要搬到 errors_legacy，回傳搬了幾筆。

    **壓成摘要而不是整筆留著**：整筆保留的代價不只是磁碟——`save_state()` 每處理
    一案就寫一次整個檔案（一輪 480 次），2,067 筆讓每輪多出約 100 MB 寫入。

    但也不是直接刪。那些項目答不出「何時」，卻仍帶著「**某案一直失敗**」這個
    訊號，而那正是新計數器要花 3 輪才能重新得出的結論。所以保留 by_permit
    次數（真正有用的那部分，筆數被建照數綁住）、錯誤分類與少量樣本。

    可重入：重複呼叫時與既有摘要累加，不會重算或清掉。
    """
    errors = state.get('errors') or []
    legacy = [e for e in errors if not (e or {}).get('at')]
    if not legacy:
        return 0

    prev = state.get(LEGACY_KEY)
    if not isinstance(prev, dict):
        # 舊版（或手動編輯）留下的 list 形式也一起折進摘要
        prev = {'count': 0, 'by_permit': {}, 'by_error': {}, 'sample': list(prev or [])}
    by_permit = dict(prev.get('by_permit') or {})
    by_error = dict(prev.get('by_error') or {})
    for e in legacy:
        permit = (e or {}).get('permit') or '（未知）'
        by_permit[permit] = by_permit.get(permit, 0) + 1
        key = str((e or {}).get('error') or '')[:_ERROR_KEY_LEN]
        by_error[key] = by_error.get(key, 0) + 1
    sample = (list(prev.get('sample') or []) + legacy)[:LEGACY_SAMPLE]
    top = sorted(by_error.items(), key=lambda kv: (-kv[1], kv[0]))[:LEGACY_TOP_ERRORS]

    state[LEGACY_KEY] = {
        'count': int(prev.get('count') or 0) + len(legacy),
        'by_permit': by_permit,
        'by_error': dict(top),
        'sample': sample,
    }
    state['errors'] = [e for e in errors if (e or {}).get('at')]
    return len(legacy)


def trim_errors(state: dict, now: datetime = None,
                keep_days: int = KEEP_ERROR_DAYS,
                max_entries: int = MAX_ERRORS) -> int:
    """修剪保留窗，回傳丟掉幾筆。只動 errors，**不動 error_streak**。

    計數器是告警的依據，修剪是為了檔案不要無限長；兩者綁在一起會讓「修剪」
    順手把「這案連續失敗 5 輪」的結論一起清掉。
    """
    now = now or datetime.now()
    errors = state.get('errors') or []
    cutoff = (now - timedelta(days=keep_days)).isoformat()
    # 無時間戳的完全排除在修剪之外——**連筆數上限也不套用**。
    # 只讓日期規則跳過它們是不夠的：正式環境有 2,067 筆無時間戳，全部通過日期
    # 規則之後會一起撞上 max_entries，被砍到只剩 500 筆。那樣「安全」就變成
    # 依賴 migrate_legacy 先跑過，是呼叫點自律而不是不變式。
    undated = [e for e in errors if not (e or {}).get('at')]
    dated = [e for e in errors if (e or {}).get('at') and str((e or {})['at']) >= cutoff]
    if max_entries and len(dated) > max_entries:
        dated = dated[-max_entries:]
    state['errors'] = undated + dated
    return len(errors) - len(state['errors'])


def finalize_run(state: dict, visited, errored, now: datetime = None,
                 run: str = None, streak_alert: int = ERROR_STREAK_ALERT,
                 completed: bool = True, run_error: str = None) -> dict:
    """本輪結束時結算連續失敗計數，回傳 {newly_stuck, recovered, visited, errored, ...}。

    **刻意在輪次層結算，不在事件層加減。** 理由：adapter 路徑會對同一案同時記下
    「部分檔案失敗」又標 processed，事件層「錯誤 +1 / 成功歸零」一旦順序顛倒就
    永遠是 0，而那是靜默的。一輪之內「有沒有出過錯」才是有定論的觀測單位。

    只結算**實際走訪過**的案：沒走訪到的既不加也不歸零（同
    feedback_negative_cache_needs_expiry：只有實際重打過的才更新時間戳）。
    同步中途掛掉時，沒走到的案不會被誤判成「這輪沒錯」。
    """
    now = now or datetime.now()
    visited, errored = set(visited or ()), set(errored or ())
    # errored 必須是 visited 的子集，否則分子分母不同源（同 #109 的教訓）
    errored &= visited
    streaks = state.setdefault('error_streak', {})
    newly_stuck, recovered = [], []
    for permit in sorted(visited):
        if permit in errored:
            streaks[permit] = streaks.get(permit, 0) + 1
            if streaks[permit] == streak_alert:
                newly_stuck.append(permit)      # 只在**跨過門檻那一輪**回報
        elif streaks.pop(permit, 0):
            recovered.append(permit)            # 先前在失敗、這輪成功 → 出聲
    state['last_run'] = {
        'at': now.isoformat(),
        'visited': len(visited),
        'errored': len(errored),
        # 「這輪有沒有跑完」必須跟著數字一起存。沒跑完的輪次，visited/errored 是
        # 截斷的樣本而不是結論——2026-10-06 掃描逾時那輪走訪 0 案，少了這個欄位
        # 就會被讀成「0 案全部無錯誤」的綠燈。
        'completed': bool(completed),
    }
    if run:
        state['last_run']['run'] = run
    if run_error:
        state['last_run']['error'] = str(run_error)[:300]
    return {'newly_stuck': newly_stuck, 'recovered': recovered,
            'visited': len(visited), 'errored': len(errored),
            'stuck': stuck_permits(state, streak_alert)}


def stuck_permits(state: dict, streak_alert: int = ERROR_STREAK_ALERT) -> list:
    """目前連續失敗已達門檻的案，次數多的在前。"""
    streaks = (state or {}).get('error_streak') or {}
    hit = [(p, n) for p, n in streaks.items() if isinstance(n, int) and n >= streak_alert]
    return sorted(hit, key=lambda kv: (-kv[1], kv[0]))


def summarise_last_run(state: dict, streak_alert: int = ERROR_STREAK_ALERT,
                       rate_alert: float = RUN_ERROR_RATE_ALERT) -> dict:
    """給 health_check 的結論。沒有 last_run 就回 None 欄位，**不回 0**。

    另外三個狀態不可被讀成綠燈：未跑完（completed False）、舊紀錄沒有 completed
    欄位（不知道）、以及走訪 0 案（逐案迴圈之前就死了，什麼都沒量到）。

    「同步還沒結算」與「結算出零錯誤」必須分得開——2026-10-05 我自己把前者讀成
    後者（step_result 時間戳是三天前、synced=0，真相是同步還在跑），
    同 feedback_counts_from_producer_not_log_prose。
    """
    last = (state or {}).get('last_run') or {}
    visited = last.get('visited')
    errored = last.get('errored')
    if not isinstance(visited, int) or not isinstance(errored, int):
        return {'known': False, 'visited': None, 'errored': None, 'rate': None,
                'systemic': False, 'stuck': stuck_permits(state, streak_alert),
                'at': last.get('at'), 'completed': None, 'run_error': None,
                'clean': False}
    rate = (errored / visited) if visited else 0.0
    # 舊紀錄沒有 completed 欄位：當成「不知道有沒有跑完」→ 不算乾淨，而不是
    # 預設成功。向後相容不可偏向綠燈那側。
    completed = last.get('completed')
    # 走訪 0 案也不是乾淨：那代表這輪在逐案迴圈之前就死了，什麼都沒量到。
    clean = bool(completed) and visited > 0 and errored == 0
    return {'known': True, 'visited': visited, 'errored': errored, 'rate': rate,
            'systemic': visited > 0 and rate >= rate_alert,
            'stuck': stuck_permits(state, streak_alert),
            'at': last.get('at'),
            'completed': completed,
            'run_error': last.get('error'),
            'clean': clean}
