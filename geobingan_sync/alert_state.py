"""告警去重與升級狀態機。

問題：health_check 每天把「同一句話」再發一次（上傳暫停連發 25 天），人會麻痺、
真正的新告警（token 過期）被淹沒。此模組讓每個告警 key 只在「新出現 / 升級
（warning→error）/ 每 remind_days 提醒一次 / 解除」時才發，其餘抑制。

核心 plan_alerts() 是純函式（不碰檔案/時間），AlertState 負責持久化到 state/alert_state.json
（state/*.json 已 gitignore，不會製造 commit 噪音）。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LEVEL_RANK = {'warning': 1, 'error': 2}
DEFAULT_REMIND_DAYS = 7


class AlertEvent:
    __slots__ = ('kind', 'key', 'level', 'message')

    def __init__(self, kind: str, key: str, level: str, message: str):
        self.kind = kind        # new | escalated | reminder | resolved
        self.key = key
        self.level = level
        self.message = message

    def __repr__(self):
        return f'AlertEvent({self.kind}, {self.key}, {self.level})'


def plan_alerts(prev: Dict[str, dict], current: Dict[str, Tuple[str, str]], now: datetime,
                remind_days: int = DEFAULT_REMIND_DAYS) -> Tuple[List[AlertEvent], Dict[str, dict]]:
    """決定這一輪要發哪些事件，並回傳更新後的狀態。

    Args:
        prev: 上次狀態 {key: {level, message, first_seen, last_sent, last_seen}}
        current: 本輪仍存在的問題 {key: (level, message)}（只含非 ok）
        now: 現在時間
        remind_days: 同一問題持續多久提醒一次

    Returns:
        (events, new_state)
    """
    events: List[AlertEvent] = []
    new_state: Dict[str, dict] = {}
    now_iso = now.isoformat()

    for key, (level, message) in current.items():
        old = prev.get(key)
        if old is None:
            events.append(AlertEvent('new', key, level, message))
            new_state[key] = {'level': level, 'message': message,
                              'first_seen': now_iso, 'last_sent': now_iso, 'last_seen': now_iso}
            continue

        entry = dict(old)
        entry.update({'level': level, 'message': message, 'last_seen': now_iso})
        escalated = LEVEL_RANK.get(level, 0) > LEVEL_RANK.get(old.get('level'), 0)
        try:
            last_sent = datetime.fromisoformat(old.get('last_sent', now_iso))
        except ValueError:
            last_sent = now
        due = (now - last_sent) >= timedelta(days=remind_days)

        if escalated:
            events.append(AlertEvent('escalated', key, level, message))
            entry['last_sent'] = now_iso
        elif due:
            events.append(AlertEvent('reminder', key, level, message))
            entry['last_sent'] = now_iso
        new_state[key] = entry

    for key, old in prev.items():
        if key not in current:
            events.append(AlertEvent('resolved', key, old.get('level', 'warning'), old.get('message', '')))

    return events, new_state


def format_events(events: List[AlertEvent], now: datetime) -> Tuple[str, str, bool]:
    """把事件組成一則留言。回傳 (title, body, needs_mention)。

    needs_mention：只要有 error 級的 new/escalated/reminder 就 @；純解除不 @。
    """
    icons = {'error': '❌', 'warning': '⚠️'}
    lines = []
    needs_mention = False
    for e in events:
        if e.kind == 'resolved':
            lines.append(f'✅ 已恢復：{e.key}')
            continue
        tag = {'new': '新', 'escalated': '升級', 'reminder': '持續'}[e.kind]
        lines.append(f'{icons.get(e.level, "❓")} [{tag}] {e.key}: {e.message}')
        if e.level == 'error':
            needs_mention = True

    active = [e for e in events if e.kind != 'resolved']
    resolved = [e for e in events if e.kind == 'resolved']
    if active and resolved:
        title = f'⚠️ geoBingAn 健康檢查：{len(active)} 個問題、{len(resolved)} 個已恢復'
    elif active:
        title = f'{"❌" if needs_mention else "⚠️"} geoBingAn 健康檢查：{len(active)} 個問題'
    else:
        title = f'✅ geoBingAn 健康檢查：{len(resolved)} 個問題已恢復'
    return title, '\n'.join(lines), needs_mention


class AlertState:
    """state/alert_state.json 的讀寫。"""

    def __init__(self, path: Optional[Path] = None):
        if path is None:
            path = Path(__file__).resolve().parent.parent / 'state' / 'alert_state.json'
        self.path = Path(path)

    def load(self) -> Dict[str, dict]:
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self, state: Dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def plan(self, current: Dict[str, Tuple[str, str]], now: Optional[datetime] = None,
             remind_days: int = DEFAULT_REMIND_DAYS) -> List[AlertEvent]:
        """只算事件、不寫狀態（乾跑/手動檢查用，不會悄悄壓掉之後的告警）。"""
        now = now or datetime.now()
        events, _ = plan_alerts(self.load(), current, now, remind_days)
        return events

    def process(self, current: Dict[str, Tuple[str, str]], now: Optional[datetime] = None,
                remind_days: int = DEFAULT_REMIND_DAYS) -> List[AlertEvent]:
        """讀狀態 → plan → 寫狀態 → 回傳要發的事件（真正發送時用）。"""
        now = now or datetime.now()
        events, new_state = plan_alerts(self.load(), current, now, remind_days)
        self.save(new_state)
        return events
