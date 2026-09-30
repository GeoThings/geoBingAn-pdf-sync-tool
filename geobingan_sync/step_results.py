"""各步驟把自己的數字寫成資料，讓 shell 不必從人話裡撈。

為什麼要這一層（2026-09-30 查出）：`run_weekly_sync.sh` 原本用 grep 從日誌撈計數，
三個都有問題，而且都是**靜默**的：

- `SYNCED_COUNT` 比對 `新增 [0-9]* 個 PDF`，但程式印的是 `更新完成: 新增 N 個`
  （沒有「PDF」兩字）→ **永遠 0**。累計 119 次執行 total_synced_pdfs 一直是 0，
  而 9/30 那一輪實際新增 2,499 份。就算樣式對得上，它還用 `tail -1` 只取最後一個
  建案，不是加總。
- `UPLOADED_COUNT` 比對 `報告上傳成功`——那句話是**後端回應文字**，被我方日誌原樣
  印出來。對方改一個字，計數就靜默歸零。
- `FAILED_COUNT` 用 `grep -c 上傳失敗`，會同時數到上傳步驟的錯誤、同步 adapter 的
  逐檔失敗、週報的「附件上傳失敗」——三件不同的事混成一個數字。

共同的根因是**讓消費端從產生端的人話裡反推數字**。人話會改、樣式會漂，而且漂掉
不會有人知道。所以改成產生端把數字寫成資料。

⚠️ 讀不到一律回 None，**不可回 0**。「沒量到」與「量到是 0」必須分得開，否則就
回到原本那種無聲歸零（同 [[沒有證據不等於健康]]）。
"""
import json
import os
from datetime import datetime

SYNC = 'sync'
UPLOAD = 'upload'
_BASE = './state'
_FILES = {SYNC: 'step_result_sync.json', UPLOAD: 'step_result_upload.json'}


def path_for(step: str, base: str = None) -> str:
    if step not in _FILES:
        raise ValueError(f'未知步驟: {step}')
    return os.path.join(base or _BASE, _FILES[step])


def write(step: str, data: dict, now: datetime = None, base: str = None) -> str:
    """原子寫入。半份 JSON 會讓讀取端回 None，那是安全的方向（未取得而非 0）。"""
    p = path_for(step, base)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    payload = dict(data)
    payload['generated_at'] = (now or datetime.now()).isoformat()
    tmp = f'{p}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return p


def read(step: str, not_before: datetime = None, base: str = None):
    """回本輪的結果，拿不到就回 None（未取得）。

    not_before＝本輪開始時間。**上一輪留下的檔案不算**——沿用會把舊數字報成
    今天的，比報「未取得」更糟：錯的數字看起來像對的。
    """
    try:
        with open(path_for(step, base), encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not_before is not None:
        raw = data.get('generated_at') or ''
        try:
            gen = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None
        if gen < not_before:
            return None
    return data


def parse_run_started(raw: str):
    """把 shell 傳來的本輪開始時間轉成 datetime；壞值回 None（＝不做新鮮度檢查）。"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        pass
    try:                                   # 也接受 epoch 秒（shell 的 date +%s）
        return datetime.fromtimestamp(int(str(raw).strip()))
    except (TypeError, ValueError, OverflowError, OSError):
        return None
