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


def _naive_local(dt: datetime):
    """統一成 naive 本地時間再比較。

    為什麼（review P1）：shell 的 `date -Iseconds` 在 macOS 會回**帶時區**的
    `2026-09-30T11:50:32+08:00`，而結果檔用 `datetime.now()` 寫的是 naive。
    直接相比會拋 `TypeError: can't compare offset-naive and offset-aware
    datetimes`，而這個例外發生在 record_sync_result 裡——整輪的狀態就記不下來，
    比計數錯還嚴重。

    ⚠️ 兩邊都要過這一關，只轉一邊等於沒轉。台北無日光節約時間，naive 本地時間
    不會有重複時刻的歧義；若日後要支援有 DST 的時區，這裡要改成一律 aware。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


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
        if _naive_local(gen) < _naive_local(not_before):
            return None
    return data


def parse_run_started(raw: str):
    """把 shell 傳來的本輪開始時間轉成 datetime；壞值回 None（＝不做新鮮度檢查）。"""
    if not raw:
        return None
    try:
        return _naive_local(datetime.fromisoformat(str(raw)))
    except (TypeError, ValueError):
        pass
    try:                                   # 也接受 epoch 秒（shell 的 date +%s）
        return datetime.fromtimestamp(int(str(raw).strip()))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


_RUN_STARTED_ENV = 'SYNC_RUN_STARTED'


def accumulate(step: str, data: dict, base: str = None, run_started: str = None,
               now: datetime = None) -> str:
    """把數字**加進本輪**的結果，而不是覆蓋。

    `main()` / `run()` 是每個城市跑一次，直接 write 會讓後一個城市蓋掉前一個的
    數字。目前只啟用台北市所以看不出來，但那是「剛好沒事」，不是正確。

    上一輪殘留的檔案不算（比對本輪開始時間），所以跨輪不會累加到舊數字。
    """
    started = parse_run_started(run_started if run_started is not None
                                else os.environ.get(_RUN_STARTED_ENV, ''))
    prev = read(step, not_before=started, base=base) or {}
    merged = {}
    for key in set(prev) | set(data):
        if key == 'generated_at':
            continue
        a, b = prev.get(key), data.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            merged[key] = a + b
        elif isinstance(a, (int, float)) and b is None:
            merged[key] = a
        else:
            merged[key] = b if b is not None else a
    return write(step, merged, now=now, base=base)
