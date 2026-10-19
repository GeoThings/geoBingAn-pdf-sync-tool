#!/usr/bin/env python3
"""送不出去的告警要留著，不可以丟掉。

2026-10-19 08:00:35 的健檢（post-sleep）`riskmap.today` 與 `api.clickup.com` 全都
解不到 DNS，**9 個要關注的問題一個都沒送出**：email 失敗、ClickUp 失敗，日誌只寫
「保留舊狀態、下輪重試」。

`AlertState.process` 的 plan→send→commit 已經做對了一半：送不出去就不落地狀態，
所以下一輪會重新產生同樣的事件。但那只在**問題還在**的時候有效——如果問題在下一輪
之前消失了，事件會變成 `resolved`，原本那則「新問題」就永遠沒有送到人手上。換句話
說，當天發生過什麼事會從帳上消失，而那正是這套告警要防的無聲失效。

所以送不出去的**通知本身**要落地排隊，下一輪（或 DNS 恢復後）照時間序補送，補送的
文字會標明原本的時間，不會讓人誤以為是現在發生的。

**有限重試的意思是「不要無限重打」，不是「逾時就丟掉」**（Zhe 2026-10-19）：每則記
`attempts`，超過 `max_attempts` 就停止自動重打並標 `stalled`，但**仍然留在佇列裡**
並由健檢的「未送達告警」項目報出來，所以它只會變得更吵、不會消失。佇列滿了也不丟最
舊的，而是把最舊的那些合併成一則摘要（寫明幾則、最舊是什麼時候）。
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

#: 佇列最多留幾則。超過就把最舊的合併成摘要，不丟掉。
DEFAULT_MAX_ITEMS = 50
#: 同一則自動重打幾次後停手（仍留在佇列並持續被報出來）。
DEFAULT_MAX_ATTEMPTS = 10
#: 摘要裡最多列幾行原標題（再多就截，並寫明省略了幾行；則數不受影響）。
DEFAULT_MAX_COALESCED_TITLES = 40

SendFn = Callable[[str, str, bool], bool]


def _parse(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None


class PendingAlerts:
    """未送達告警的持久佇列，一個 producer 一個 namespace（與 AlertState 同慣例）。"""

    def __init__(self, path: Optional[Path] = None, namespace: str = 'default',
                 max_items: int = DEFAULT_MAX_ITEMS,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 max_coalesced_titles: int = DEFAULT_MAX_COALESCED_TITLES):
        if path is None:
            path = (Path(__file__).resolve().parent.parent / 'state'
                    / f'pending_alerts_{namespace}.json')
        self.path = Path(path)
        self.namespace = namespace
        self.max_items = max_items
        self.max_attempts = max_attempts
        self.max_coalesced_titles = max_coalesced_titles

    # ---------- 讀寫 ----------

    def load(self) -> List[dict]:
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return [i for i in data if isinstance(i, dict)] if isinstance(data, list) else []

    def save(self, items: List[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    # ---------- 入列 ----------

    def add(self, title: str, body: str, needs_attention: bool,
            now: Optional[datetime] = None, error: str = '',
            keys: Optional[List[str]] = None) -> dict:
        """把一則送不出去的通知排進佇列。回傳剛入列的那一則。

        `keys` 是這則通知講了哪些告警 key。下一輪若那些 key 的事件重新產生並成功
        送出，這一則就是重複的（見 `resolve_covered`）。
        """
        now = now or datetime.now()
        item = {'queued_at': now.isoformat(), 'title': title, 'body': body,
                'needs_attention': bool(needs_attention), 'attempts': 0,
                'last_attempt': None, 'last_error': str(error)[:300],
                'keys': sorted(set(keys or []))}
        items = self.load()
        items.append(item)
        self.save(self._cap(items))
        print(f"  📥 告警已排進待送佇列（{self.namespace}，目前 {len(self.load())} 則）")
        return item

    def _cap(self, items: List[dict]) -> List[dict]:
        """超過上限時把最舊的合併成一則摘要——不丟掉，也不假裝沒發生過。

        兩個地方容易寫錯，都被測試釘住了：
        1. **合併的合併**：摘要再被合併時，`coalesced` 要把它已經代表的則數加進來，
           否則數字會越合併越小、最後報出的「幾則」是假的。
        2. **標題要跟著進去**：只留則數等於把內容丟了。所以摘要帶 `coalesced_titles`，
           再次合併時整串接上去；真的太長才截，而且會寫明省略了幾行。
        """
        if len(items) <= self.max_items:
            return items
        cut = len(items) - self.max_items + 1
        overflow, kept = items[:cut], items[cut:]
        merged, lines = 0, []
        for item in overflow:
            merged += int(item.get('coalesced') or 1)
            prev = item.get('coalesced_titles')
            if prev:
                lines.extend(prev)
            else:
                lines.append(f'· {item.get("queued_at", "?")} {item.get("title", "")}')
        omitted = max(0, len(lines) - self.max_coalesced_titles)
        if omitted:
            lines = ([f'· （更早的 {omitted} 行標題已省略，但仍計入下面的則數）']
                     + lines[-self.max_coalesced_titles:])
        oldest = overflow[0].get('queued_at', '（不明）')
        summary = {
            'queued_at': oldest,
            'title': f'⚠️ geoBingAn 健康檢查：{merged} 則更早的告警未送達（已合併）',
            'body': (f'佇列已達上限（{self.max_items} 則），以下 {merged} 則被合併成這一筆'
                     f'（最舊 {oldest}）：\n' + '\n'.join(lines)),
            'needs_attention': any(i.get('needs_attention') for i in overflow),
            'attempts': max((i.get('attempts') or 0) for i in overflow),
            'last_attempt': None,
            'last_error': 'coalesced_on_overflow',
            'coalesced': merged,
            'coalesced_titles': lines,
            'keys': sorted({k for i in overflow for k in (i.get('keys') or [])}),
        }
        return [summary] + kept

    # ---------- 已被新通知涵蓋就不要再送一次 ----------

    def resolve_covered(self, sent_keys, now: Optional[datetime] = None) -> int:
        """丟掉那些「內容已經被剛剛成功送出的通知講過」的積壓，避免重複通知。

        為什麼需要：送達失敗時狀態不落地，所以**問題還在**的話下一輪會重新產生
        同樣的事件、把當前狀況講一次。這時積壓那一則是同一件事的舊版本，再補送
        就是同一件事通知兩次。

        只有 key 完全被涵蓋才丟：若積壓那則講了 A、B 而新通知只講了 A，B 的資訊
        還沒送到，整則留著（寧可重複一點點，也不要漏掉 B）。沒記 key 的（舊格式）
        一律保留——判斷不了就不丟。
        """
        sent = set(sent_keys or ())
        if not sent:
            return 0
        items, kept, dropped = self.load(), [], 0
        for item in items:
            keys = set(item.get('keys') or ())
            if keys and keys <= sent:
                dropped += 1
                continue
            kept.append(item)
        if dropped:
            self.save(kept)
            print(f"  ✅ {dropped} 則積壓告警的內容已由本輪通知涵蓋，不再重複補送")
        return dropped

    # ---------- 補送 ----------

    def flush(self, send: SendFn, now: Optional[datetime] = None) -> Tuple[int, int]:
        """照時間序補送。只有 send 回報成功才移除（與 AlertState 同一條紀律）。

        Returns: (送出幾則, 還剩幾則)
        """
        items = self.load()
        if not items:
            return 0, 0
        now = now or datetime.now()
        items.sort(key=lambda i: str(i.get('queued_at') or ''))
        sent, remaining = 0, []
        stopped = False
        for item in items:
            attempts = item.get('attempts') or 0
            if stopped or attempts >= self.max_attempts:
                # 超過次數就不再自動重打，但**留著**並標記，讓它繼續被報出來
                item['stalled'] = True
                remaining.append(item)
                continue
            title, body = self._as_backlog(item, now)
            try:
                ok = bool(send(title, body, bool(item.get('needs_attention'))))
            except Exception as e:                                  # noqa: BLE001
                item['last_error'] = f'{type(e).__name__}: {e}'[:300]
                ok = False
            item['attempts'] = attempts + 1
            item['last_attempt'] = now.isoformat()
            if ok:
                sent += 1
                continue
            item['stalled'] = item['attempts'] >= self.max_attempts
            remaining.append(item)
            # 這一輪的通道顯然不通，不用把剩下的也各打一次
            stopped = True
        self.save(remaining)
        if sent:
            print(f"  📤 補送 {sent} 則先前未送達的告警（{self.namespace}）")
        if remaining:
            print(f"  📥 仍有 {len(remaining)} 則未送達（{self.namespace}），下一輪再試")
        return sent, len(remaining)

    def _as_backlog(self, item: dict, now: datetime) -> Tuple[str, str]:
        """補送的文字要標明原本的時間，否則讀者會以為是現在發生的。"""
        queued = _parse(item.get('queued_at'))
        when = f'{queued:%Y-%m-%d %H:%M}' if queued else '時間不明'
        age = ''
        if queued:
            hours = (now - queued).total_seconds() / 3600
            age = f'，距今 {hours:.0f} 小時' if hours >= 1 else '，不到 1 小時前'
        title = f'[補送 {when}] {item.get("title", "")}'
        body = (f'這是 {when} 當時送不出去的告警（補送{age}；'
                f'已嘗試 {item.get("attempts") or 0} 次，原因：{item.get("last_error") or "不明"}）。\n'
                f'**內容是當時的狀況，不是現在的。**\n\n{item.get("body", "")}')
        return title, body

    # ---------- 給健檢報出來 ----------

    def summary(self, now: Optional[datetime] = None) -> Dict[str, object]:
        """{'count', 'oldest_age_hours', 'stalled', 'coalesced'}；count 0 代表佇列空的。"""
        items = self.load()
        now = now or datetime.now()
        oldest_age = None
        for item in items:
            queued = _parse(item.get('queued_at'))
            if queued is None:
                continue
            age = (now - queued).total_seconds() / 3600
            oldest_age = age if oldest_age is None else max(oldest_age, age)
        return {'count': len(items),
                'oldest_age_hours': oldest_age,
                'stalled': sum(1 for i in items if i.get('stalled')),
                'coalesced': sum(int(i.get('coalesced') or 0) for i in items)}
