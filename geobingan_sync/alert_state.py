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
from typing import Callable, Dict, List, Optional, Tuple

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
    """把事件組成一則留言。回傳 (title, body, needs_attention)。

    needs_attention＝這批事件需不需要「打擾人」：**error 級的 new/escalated/reminder，
    以及從 error 恢復（resolved）都算**。error 恢復也要通知到人，否則承諾的「恢復
    通知」等於沒有——ClickUp 對 token 擁有者本人不會推播（review P2）。

    為何是單一旗標而非 needs_mention／needs_email 兩個：兩者在所有情境下恆等
    （warning 一律不打擾、error 含恢復一律要打擾），且 ClickUp 的 @ 對本人是空
    操作，真正的決策只有「要不要打擾人」這一件事。留兩個永遠相等的布林只會讓
    呼叫端誤以為可以分開調。
    """
    icons = {'error': '❌', 'warning': '⚠️'}
    lines = []
    needs_attention = False
    for e in events:
        if e.kind == 'resolved':
            lines.append(f'✅ 已恢復：{e.key}')
            if e.level == 'error':
                needs_attention = True      # error 解除也要送到人
            continue
        tag = {'new': '新', 'escalated': '升級', 'reminder': '持續'}[e.kind]
        lines.append(f'{icons.get(e.level, "❓")} [{tag}] {e.key}: {e.message}')
        if e.level == 'error':
            needs_attention = True

    active = [e for e in events if e.kind != 'resolved']
    resolved = [e for e in events if e.kind == 'resolved']
    if active and resolved:
        title = f'⚠️ geoBingAn 健康檢查：{len(active)} 個問題、{len(resolved)} 個已恢復'
    elif active:
        title = f'{"❌" if needs_attention else "⚠️"} geoBingAn 健康檢查：{len(active)} 個問題'
    else:
        title = f'✅ geoBingAn 健康檢查：{len(resolved)} 個問題已恢復'
    return title, '\n'.join(lines), needs_attention


SendFn = Callable[[str, str, bool], bool]   # send(title, body, needs_attention) -> 是否送達


class AlertState:
    """告警狀態的讀寫，一個 producer 一個 namespace（各自獨立檔）。

    為何要分檔：health_check 與 record_sync_result 各自只知道自己的 key；若共用
    一份狀態，任一方都會把對方的 key 當成「已恢復」刪掉並誤發 ✅（review P1）。
    """

    def __init__(self, path: Optional[Path] = None, namespace: str = 'default'):
        if path is None:
            path = Path(__file__).resolve().parent.parent / 'state' / f'alert_state_{namespace}.json'
        self.path = Path(path)
        self.namespace = namespace

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

    def process(self, current: Dict[str, Tuple[str, str]], send: SendFn,
                now: Optional[datetime] = None,
                remind_days: int = DEFAULT_REMIND_DAYS) -> Tuple[List[AlertEvent], bool]:
        """plan → send → commit：只有 send 回報成功才寫入新狀態（含 last_sent）。

        發送失敗（回傳 falsy 或拋例外）時保留舊狀態，下一輪會再產生同樣的事件重試，
        不會被當成「已發過」壓 7 天（review P1）。沒有事件時只更新 last_seen/message。

        Returns:
            (events, delivered)
        """
        now = now or datetime.now()
        events, new_state = plan_alerts(self.load(), current, now, remind_days)
        if not events:
            self.save(new_state)
            return events, True
        title, body, needs_attention = format_events(events, now)
        try:
            delivered = bool(send(title, body, needs_attention))
        except Exception as e:
            print(f"  告警發送例外（狀態不落地，下輪重試）: {e}")
            delivered = False
        if delivered:
            self.save(new_state)
        else:
            print(f"  告警未送達（{self.namespace}），保留舊狀態、下輪重試")
        return events, delivered
