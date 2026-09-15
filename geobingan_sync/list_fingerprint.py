"""政府清單指紋：偵測清單更新、疑似停更，以及「靜默退回靜態舊清單」。

PR #78 讓 sync 先從建管處發布頁動態解析當前清單連結，失敗才退回寫死的靜態
網址。但退回只印一行 log、沒有任何告警——建管處哪天改版導致解析失效，我們會
**靜默**同步 2025 年的舊清單，正是當初 8 個月沒人發現的情境。

本模組把每次同步的清單身分記在 state/list_fingerprint.json：
- source：'動態' 或 '靜態'（靜態 = 動態解析失效，health_check 會報 error）
- label：檔名（通常帶民國日期，如 表單回復_1150902.pdf）
- permit_count / sha256：以「排序後的建照號集合」計算，忽略 PDF 重新編碼的雜訊
- last_changed：內容最後一次真正變動的時間（超過門檻未變 = 疑似停更）
"""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Tuple

STALE_DAYS = 60          # 指紋超過這麼久沒變 → 疑似停更
DEFAULT_FILENAME = 'list_fingerprint.json'


def compute_digest(permits: Iterable[str]) -> str:
    """以排序後的建照號集合計算 sha256（語意指紋，不受 PDF 重新編碼影響）。"""
    joined = '\n'.join(sorted(set(permits)))
    return hashlib.sha256(joined.encode('utf-8')).hexdigest()


def diff_permits(prev: Iterable[str], curr: Iterable[str]) -> Tuple[list, list]:
    """回傳 (新增, 移除)，皆為排序後的建照號。"""
    p, c = set(prev or []), set(curr or [])
    return sorted(c - p), sorted(p - c)


def format_change(label: str, source: str, added: list, removed: list, total: int) -> str:
    """組一段人看得懂的變更摘要。"""
    parts = [f'清單已更新：{label or "(無檔名)"}（{source}，共 {total} 筆建照）']
    if added:
        parts.append(f'新增 {len(added)} 筆，例如 {"、".join(added[:3])}')
    if removed:
        parts.append(f'移除 {len(removed)} 筆（多為完工下架），例如 {"、".join(removed[:3])}')
    return '；'.join(parts)


class ListFingerprint:
    """state/list_fingerprint.json 的讀寫。"""

    def __init__(self, path: Optional[Path] = None):
        if path is None:
            path = Path(__file__).resolve().parent.parent / 'state' / DEFAULT_FILENAME
        self.path = Path(path)

    def load(self) -> dict:
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self, data: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f'{self.path.name}.{os.getpid()}.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        return data

    def update(self, label: str, source: str, permits: Iterable[str],
               now: Optional[datetime] = None) -> Tuple[bool, Optional[str], dict]:
        """記錄本次清單身分。

        Returns:
            (changed, summary, state)
            changed：內容指紋是否與上次不同（首次建立基線時為 False，避免無意義通知）
            summary：changed 為 True 時的人類可讀摘要，否則 None
        """
        now = now or datetime.now()
        permits = sorted(set(permits or []))
        digest = compute_digest(permits)
        prev = self.load()
        first_time = not prev.get('sha256')
        changed = bool(prev.get('sha256')) and prev.get('sha256') != digest

        summary = None
        pending = list(prev.get('pending_notices') or [])
        if changed:
            added, removed = diff_permits(prev.get('permits', []), permits)
            summary = format_change(label, source, added, removed, len(permits))
            # 變更通知先存成 pending，送達才清除（review P2）：若直接在送出前就把
            # 指紋存成新 hash，下一輪 changed=False，這次的變更通知會永久遺失。
            pending.append(summary)

        state = {
            'source': source,
            'label': label,
            'permit_count': len(permits),
            'sha256': digest,
            'permits': permits,
            'last_checked': now.isoformat(),
            'last_changed': now.isoformat() if (changed or first_time) else prev.get('last_changed', now.isoformat()),
            'last_change_summary': summary or prev.get('last_change_summary'),
            'pending_notices': pending,
        }
        return changed, summary, self.save(state)


    def clear_pending(self, now: Optional[datetime] = None) -> dict:
        """通知確定送達後才呼叫，清掉待送佇列。"""
        data = self.load()
        data['pending_notices'] = []
        data['last_checked'] = (now or datetime.now()).isoformat()
        return self.save(data)


def assess(state: dict, now: Optional[datetime] = None,
           stale_days: int = STALE_DAYS) -> Tuple[str, str]:
    """把指紋狀態判成 health_check 的 (level, message)。純函式，便於測試。

    - 尚無基線 → ok（下次同步後建立）
    - source 非「動態」→ error：動態解析失效、正在同步可能過期的靜態清單
    - last_changed 超過 stale_days → warning：清單疑似停更
    """
    now = now or datetime.now()
    if not state or not state.get('sha256'):
        return 'ok', '清單指紋尚未建立（下次同步後產生）'

    label = state.get('label') or '(無檔名)'
    count = state.get('permit_count', 0)
    source = state.get('source', '?')

    if source != '動態':
        return 'error', (f'正在使用靜態備援清單（{label}，{count} 筆）—— 發布頁動態解析失效，'
                         f'可能同步到過期清單，請檢查建管處發布頁是否改版')

    try:
        changed_at = datetime.fromisoformat(state.get('last_changed', ''))
    except ValueError:
        return 'ok', f'清單 {label}（{count} 筆，{source}）'

    days = (now - changed_at).days
    if days >= stale_days:
        return 'warning', (f'清單 {label}（{count} 筆）已 {days} 天未更新，疑似停更；'
                           f'請確認建管處是否仍在維護')
    return 'ok', f'清單 {label}（{count} 筆，{source}，{days} 天前更新）'
