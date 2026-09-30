r"""政府清單指紋：偵測清單更新、疑似停更，以及「靜默退回靜態舊清單」。

PR #78 讓 sync 先從建管處發布頁動態解析當前清單連結，失敗才退回寫死的靜態
網址。但退回只印一行 log、沒有任何告警——建管處哪天改版導致解析失效，我們會
**靜默**同步 2025 年的舊清單，正是當初 8 個月沒人發現的情境。

本模組把每次同步的清單身分記在 state/list_fingerprint.json：
- source：'動態' 或 '靜態'（靜態 = 動態解析失效，health_check 會報 error）
- label：檔名（通常帶民國日期，如 表單回復_1150902.pdf）
- permit_count / sha256：以「排序後的建照號集合」計算，忽略 PDF 重新編碼的雜訊
- last_changed：內容最後一次真正變動的時間（超過門檻未變 = 疑似停更）
- basis：指紋的計算基準（見下）

⚠️ 指紋必須只反映**對方的清單**，不可混入我方的解析能力（2026-09-30 實際踩到）。
原本取的是「解析出連結的建照」，於是 PR #99 讓解析改抓任何 http(s) 連結之後，
集合從 368 變 440、指紋跟著變，機制就發出「清單已更新，新增 72 筆」——政府那邊
一個字都沒改，新增的 72 筆全部是我們原本丟掉的非 Drive 連結。

後果不只是一則假通知：`last_changed` 被推到當天，而「疑似停更」是靠這個欄位判定，
等於我們自己把停更偵測的時鐘重置了。反方向更糟——解析若退化而漏掉建照，會報
「移除 N 筆」，看起來像政府下架了建案。

所以改成取「PDF 裡實際存在的建照號」（用 `\d{2,3}建字第\d{3,5}號` 在全文找，
不受連結解析影響）。涵蓋率屬於我方能力，記在 unsupported_sources 名單。
`basis` 欄位記下計算基準；基準變更時走**遷移**而非「清單已更新」，見 update()。
"""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Tuple

class EmptyPermitSet(ValueError):
    """已有基線卻抽不到任何建照號——解析壞了，不是政府清空了清單。

    fail-closed 的理由（review P1）：若照寫下去，會①誤報「移除 440 筆」並排入通知
    ②把基線覆蓋成空的，下一輪再誤報「新增 440 筆」③把 last_changed 推到當天、
    重設停更時鐘。三個後果都比「同步這一步失敗」嚴重得多，所以拋例外讓呼叫端整步
    失敗（shell 會記 error 並告警），保住基線。

    這與本模組既有的紀律一致：指紋是 fail-closed 的核心狀態，寧可大聲失敗，
    不要留下看起來正常的錯資料。
    """


STALE_DAYS = 60          # 指紋超過這麼久沒變 → 疑似停更
DEFAULT_FILENAME = 'list_fingerprint.json'

#: 目前的指紋基準：清單 PDF 裡實際存在的建照號。
BASIS_PERMITS_IN_PDF = 'permits_in_pdf'
#: 舊基準（檔案裡沒有 basis 欄位者）：只含解析出連結的建照。
BASIS_LEGACY_WITH_LINKS = 'permits_with_links'


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
               now: Optional[datetime] = None, basis: str = BASIS_PERMITS_IN_PDF,
               permits_with_links: Optional[int] = None) -> Tuple[bool, Optional[str], dict]:
        """記錄本次清單身分。

        Returns:
            (changed, summary, state)
            changed：內容指紋是否與上次不同（首次建立基線、或基準遷移時為 False）
            summary：changed 為 True 時的人類可讀摘要，否則 None

        **基準遷移**：指紋的計算基準若與檔案裡記的不同（例如從舊的「解析出連結的
        建照」換成「PDF 裡的建照」），指紋必然改變，但那不是對方改了清單。此時
        一律不報「清單已更新」、**不推進 last_changed**（否則會把疑似停更的時鐘
        重置），只記下新基準與遷移時間。
        """
        now = now or datetime.now()
        permits = sorted(set(permits or []))
        prev = self.load()
        first_time = not prev.get('sha256')

        # 已有基線卻一筆都抽不到 → 解析壞了，不是政府清空清單。在**寫入之前**擋下。
        if not permits and prev.get('permits'):
            raise EmptyPermitSet(
                f'清單解析未取得任何建照號，但基線有 {len(prev["permits"])} 筆——'
                f'研判為 PDF 文字抽取或建照正則失效。指紋不予更新以保住基線；'
                f'請檢查 {prev.get("label") or "清單 PDF"} 是否改版')

        digest = compute_digest(permits)
        prev_basis = prev.get('basis') or BASIS_LEGACY_WITH_LINKS
        migrated = (not first_time) and prev_basis != basis
        changed = (bool(prev.get('sha256')) and prev.get('sha256') != digest
                   and not migrated)

        summary = None
        pending = list(prev.get('pending_notices') or [])
        discarded = []
        if migrated and pending:
            # 舊基準留下的待送通知無法在新基準下驗證（9/30 那則假通知正是這樣產生
            # 的），送出去等於把已知可疑的訊息推給操作者。丟棄但不靜默：留在 state
            # 裡並由呼叫端印出（review P2）。
            discarded, pending = pending, []
        if changed:
            added, removed = diff_permits(prev.get('permits', []), permits)
            summary = format_change(label, source, added, removed, len(permits))
            # 變更通知先存成 pending，送達才清除（review P2）：若直接在送出前就把
            # 指紋存成新 hash，下一輪 changed=False，這次的變更通知會永久遺失。
            pending.append(summary)

        state = {
            'source': source,
            'label': label,
            'basis': basis,
            'permit_count': len(permits),
            'sha256': digest,
            'permits': permits,
            'last_checked': now.isoformat(),
            'last_changed': now.isoformat() if (changed or first_time) else prev.get('last_changed', now.isoformat()),
            'last_change_summary': summary or prev.get('last_change_summary'),
            'pending_notices': pending,
        }
        if permits_with_links is not None:
            # 純資訊：我方解析出連結的數量。**不參與指紋**，否則又把涵蓋率混進來。
            state['permits_with_links'] = int(permits_with_links)
        if migrated:
            state['basis_migrated_at'] = now.isoformat()
            state['basis_migrated_from'] = prev_basis
            if discarded:
                state['basis_migration_discarded_notices'] = discarded
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
