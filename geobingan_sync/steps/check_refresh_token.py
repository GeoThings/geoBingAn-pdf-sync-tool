#!/usr/bin/env python3
"""判讀 refresh token 的狀態，並以 exit code 回報給 run_weekly_sync.sh。

為什麼要獨立成一個模組（PR #125 第一輪 review 的 P2）：原本這段判讀是寫在
shell 裡的 inline python——

    exp = payload.get('exp', 0)
    days_left = (exp - time.time()) / 86400

`exp` 不是數字時那一行拋 TypeError，python 以 **exit 1** 結束，而腳本把 1 解讀成
「快過期（<2 天）」，於是照樣放行三個需要 v2 的步驟。也就是說「讀不出來」被
偽裝成「還能用」。

這和 #124 的 `socket.gaierror` 是同一種錯：**用我們想像中的失敗模式當代理，
真正的失敗模式沒被涵蓋**。所以：

- 判讀搬到可測試的地方（inline python 測不到）。
- 「讀不出來」有**自己的** exit code，不跟任何一個「還能用」的狀態共用。
- 沒有 `exp` 不等於已過期。舊寫法 `.get('exp', 0)` 會把它算成 1970 年、報
  「已過期 20000 天」——那是編造出來的數字。
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from geobingan_sync.jwt_auth import decode_jwt_payload

# exit code 是 shell 唯一讀得到的東西，所以它本身就是介面。
EXIT_CODES = {
    'ok': 0,
    'warning': 1,        # 剩不到 WARNING_DAYS 天：提醒，不是停工理由
    'expired': 2,        # 已過期
    'unreadable': 3,     # 讀不出來——**不可以**和 warning 共用 code
}

WARNING_DAYS = 2


def classify(token: str, now: float = None, warning_days: float = WARNING_DAYS):
    """回 (state, days_left, detail)。

    state 是 EXIT_CODES 的鍵。`days_left` 在 unreadable 時是 None——沒量到就
    不要給數字，給了就會被當成真的。
    """
    now = time.time() if now is None else now
    payload = decode_jwt_payload(token or '')
    if not isinstance(payload, dict) or not payload:
        return 'unreadable', None, 'payload 解不出來（token 格式不對或不是 JWT）'

    exp = payload.get('exp')
    if exp is None:
        return 'unreadable', None, 'payload 裡沒有 exp'
    # bool 是 int 的子類，True 會被算成 1 秒（1970 年），所以要先擋掉
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return 'unreadable', None, f'exp 不是數字：{exp!r}'

    # 「是數字」還不夠，數字本身也要能用（review r2 的 P2）：
    #  - `1e309` 經 json 解析是 inf，而 inf 會讓它判成「ok、剩 inf 天」
    #    ——一張永不過期的 token。
    #  - `10**400` 是任意精度整數，float() 會拋 OverflowError；原本沒捕捉，
    #    例外逃出去就變成 CLI exit 1，又被 shell 當成「快過期」。
    try:
        exp_seconds = float(exp)
    except (OverflowError, ValueError) as e:
        return 'unreadable', None, f'exp 轉不成浮點數（{type(e).__name__}）：{exp!r}'
    if not math.isfinite(exp_seconds):
        return 'unreadable', None, f'exp 不是有限值：{exp_seconds!r}'

    days_left = (exp_seconds - now) / 86400
    if days_left <= 0:
        return 'expired', days_left, f'已過期 {-days_left:.1f} 天'
    if days_left < warning_days:
        return 'warning', days_left, f'剩 {days_left:.1f} 天'
    return 'ok', days_left, f'剩 {days_left:.1f} 天'


def main() -> int:
    # 兜底：任何沒預料到的失敗都回 unreadable 的 code。
    # 這一層不是裝飾——「例外 → exit 1 → 被當成快過期 → 放行 v2」已經發生過
    # 三次（inline python 的 TypeError、#124 的 socket.gaierror、這次的
    # OverflowError）。單獨修每個案例只會等下一個；把**整個類別**關掉。
    try:
        return _main()
    except Exception as e:
        print(f'UNREADABLE:判讀時發生未預期的錯誤（{type(e).__name__}）：{e}')
        return EXIT_CODES['unreadable']


def _main() -> int:
    from geobingan_sync.config import REFRESH_TOKEN
    state, days_left, detail = classify(REFRESH_TOKEN)
    if state == 'unreadable':
        print(f'UNREADABLE:{detail}')
    elif state == 'expired':
        print(f'EXPIRED:{-days_left:.1f}')
    elif state == 'warning':
        print(f'WARNING:{days_left:.1f}')
    else:
        print(f'OK:{days_left:.1f}')
    return EXIT_CODES[state]


if __name__ == '__main__':
    sys.exit(main())
