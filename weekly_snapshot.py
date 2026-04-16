#!/usr/bin/env python3
"""
週報快照管理

每次同步完成後儲存快照，供週報趨勢分析使用。
也負責偵測新建案並發送通知。

用法：
  python3 weekly_snapshot.py              # 儲存快照 + 偵測新建案
  python3 weekly_snapshot.py --notify      # 有新建案時發送通知
  python3 weekly_snapshot.py --diff        # 顯示與上次快照的差異
"""

import json
import os
import sys
import argparse
from datetime import datetime
from pathlib import Path

SNAPSHOT_DIR = './state/weekly_snapshots'
STATE_DIR = './state'


def save_snapshot():
    """儲存本週快照"""
    with open(f'{STATE_DIR}/permit_system_mapping.json', 'r', encoding='utf-8') as f:
        mapping = json.load(f)
    with open(f'{STATE_DIR}/permit_registry.json', 'r', encoding='utf-8') as f:
        registry = json.load(f)

    # 彙整統計
    statuses = {}
    for d in mapping.values():
        s = d.get('status', '')
        statuses[s] = statuses.get(s, 0) + 1

    alerts_confirmed = 0
    for p, info in registry.items():
        la = info.get('live_alerts', {})
        if la and la.get('total', 0) > 0 and mapping.get(p, {}).get('system_count', 0) > 0:
            alerts_confirmed += 1

    snapshot = {
        'date': datetime.now().strftime('%Y-%m-%d'),
        'total_permits': len(mapping),
        'total_pdfs': sum(d.get('drive_count', 0) for d in mapping.values()),
        'total_ai': sum(d.get('system_count', 0) for d in mapping.values()),
        'named_permits': sum(1 for e in registry.values() if e.get('name')),
        'api_matched': sum(1 for e in registry.values() if e.get('api_match')),
        'alerts_confirmed': alerts_confirmed,
        'statuses': statuses,
        'permits': sorted(mapping.keys()),
    }

    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    filename = f'{SNAPSHOT_DIR}/{snapshot["date"]}.json'
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    print(f"📸 快照已儲存: {filename}")
    return snapshot


def get_previous_snapshot():
    """取得最近一次的快照"""
    if not os.path.exists(SNAPSHOT_DIR):
        return None
    files = sorted(Path(SNAPSHOT_DIR).glob('*.json'), reverse=True)
    # 跳過今天的
    today = datetime.now().strftime('%Y-%m-%d')
    for f in files:
        if f.stem != today:
            with open(f, 'r', encoding='utf-8') as fp:
                return json.load(fp)
    return None


def compute_diff(current, previous):
    """計算兩次快照的差異"""
    if not previous:
        return None

    curr_permits = set(current.get('permits', []))
    prev_permits = set(previous.get('permits', []))

    diff = {
        'period': f"{previous['date']} → {current['date']}",
        'new_permits': sorted(curr_permits - prev_permits),
        'removed_permits': sorted(prev_permits - curr_permits),
        'total_change': current['total_permits'] - previous['total_permits'],
        'pdfs_change': current['total_pdfs'] - previous['total_pdfs'],
        'ai_change': current['total_ai'] - previous['total_ai'],
        'named_change': current['named_permits'] - previous['named_permits'],
        'alerts_change': current['alerts_confirmed'] - previous['alerts_confirmed'],
    }
    return diff


def format_diff(diff):
    """格式化差異報告"""
    if not diff:
        return "（無前次快照可比較）"

    def arrow(n):
        if n > 0: return f'↑{n}'
        if n < 0: return f'↓{abs(n)}'
        return '→ 不變'

    lines = [
        f"📊 趨勢分析（{diff['period']}）",
        f"  建案數：{arrow(diff['total_change'])}",
        f"  雲端報告：{arrow(diff['pdfs_change'])}",
        f"  AI 分析：{arrow(diff['ai_change'])}",
        f"  名稱覆蓋：{arrow(diff['named_change'])}",
        f"  監測警戒：{arrow(diff['alerts_change'])}",
    ]

    if diff['new_permits']:
        lines.append(f"\n🆕 新增建案（{len(diff['new_permits'])} 個）：")
        for p in diff['new_permits'][:10]:
            lines.append(f"  • {p}")
        if len(diff['new_permits']) > 10:
            lines.append(f"  ...及其他 {len(diff['new_permits'])-10} 個")

    return '\n'.join(lines)


def notify_new_permits(diff):
    """新建案通知"""
    if not diff or not diff['new_permits']:
        return

    count = len(diff['new_permits'])
    permits_text = '\n'.join(f'• {p}' for p in diff['new_permits'][:20])
    message = f"🆕 偵測到 {count} 個新建案\n\n{permits_text}"

    # LINE Notify
    try:
        from notify import send_notification
        send_notification(f'🆕 geoBingAn 新增 {count} 個建案', message)
        print(f"  通知已發送（{count} 個新建案）")
    except Exception as e:
        print(f"  通知發送失敗: {e}")

    # ClickUp comment
    try:
        import requests
        clickup_token = os.environ.get('CLICKUP_TOKEN', '')
        requests.post(
            'https://api.clickup.com/api/v2/task/86ex8u782/comment',
            headers={'Authorization': clickup_token, 'Content-Type': 'application/json'},
            json={'comment_text': message},
            timeout=10
        )
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description='週報快照管理')
    parser.add_argument('--notify', action='store_true', help='新建案時發送通知')
    parser.add_argument('--diff', action='store_true', help='顯示與上次快照的差異')
    args = parser.parse_args()

    # 儲存快照
    current = save_snapshot()

    # 取得前次快照並比較
    previous = get_previous_snapshot()
    diff = compute_diff(current, previous)

    if diff:
        print(format_diff(diff))
    else:
        print("（首次快照，無前次資料可比較）")

    # 通知新建案
    if args.notify and diff and diff['new_permits']:
        notify_new_permits(diff)

    # 回傳 diff 供週報使用
    return diff


if __name__ == '__main__':
    main()
