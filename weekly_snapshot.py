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
DRIVE_CACHE_FILE = './state/uploaded_to_geobingan_7days.json'
MONTHLY_ALERT_STATE = './state/monthly_activity_alert.json'

# 月度活動告警門檻：當月報告數 < 前 3 月平均 × 此倍率 → 警告
MONTHLY_DROP_THRESHOLD = 0.3
# 前 3 月平均低於此值就不警告（樣本太小）
MONTHLY_MIN_BASELINE = 10


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
        from config import CLICKUP_TOKEN as clickup_token
        if not clickup_token:
            return
        requests.post(
            'https://api.clickup.com/api/v2/task/86ex8u782/comment',
            headers={'Authorization': clickup_token, 'Content-Type': 'application/json'},
            json={'comment_text': message},
            timeout=10
        )
    except Exception as e:
        print(f"  ⚠️ ClickUp 通知失敗: {e}")


def _bin_pdfs_by_report_month(pdfs):
    """從檔名解析報告日期，依年月計數。"""
    from collections import Counter
    from filename_date_parser import parse_date_from_filename
    by_month = Counter()
    for p in pdfs:
        dt = parse_date_from_filename(p.get('name', ''))
        if dt:
            by_month[(dt.year, dt.month)] += 1
    return by_month


def _months_back(y: int, m: int, n: int):
    """回推 n 個月。"""
    m -= n
    while m < 1:
        m += 12
        y -= 1
    return y, m


def check_monthly_activity_trend(notify: bool = False):
    """偵測上一個月的監測報告數是否異常下滑。

    動機：2026-04 觀察到工地監測 PDF 數從 3 月 55 暴跌到 11，雖然個別小變化看不出來，
    但月度匯總一目瞭然。完工退場、法規變動、合規鬆動都可能造成此類訊號。
    """
    if not os.path.exists(DRIVE_CACHE_FILE):
        return
    try:
        with open(DRIVE_CACHE_FILE, 'r', encoding='utf-8') as f:
            pdfs = json.load(f).get('cache', {}).get('pdfs', [])
    except (json.JSONDecodeError, IOError):
        return
    if not pdfs:
        return

    by_month = _bin_pdfs_by_report_month(pdfs)
    today = datetime.now()
    last_y, last_m = _months_back(today.year, today.month, 1)
    last_label = f'{last_y}-{last_m:02d}'
    last_count = by_month.get((last_y, last_m), 0)

    prior_keys = [_months_back(last_y, last_m, n) for n in range(1, 4)]
    prior_counts = [by_month.get(k, 0) for k in prior_keys]
    prior_avg = sum(prior_counts) / len(prior_counts)

    print(f"\n📊 月度監測報告：{last_label} = {last_count}，前 3 月平均 = {prior_avg:.1f}")

    if prior_avg < MONTHLY_MIN_BASELINE:
        return  # 樣本太小不警告
    if last_count >= MONTHLY_DROP_THRESHOLD * prior_avg:
        return  # 在正常範圍

    # 同一個月只警告一次
    state = {}
    if os.path.exists(MONTHLY_ALERT_STATE):
        try:
            with open(MONTHLY_ALERT_STATE, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    if state.get('last_alerted_month') == last_label:
        return

    drop_pct = (1 - last_count / prior_avg) * 100
    prior_detail = ', '.join(
        f'{y}-{m:02d}={by_month.get((y, m), 0)}'
        for y, m in reversed(prior_keys)
    )
    msg = (
        f"{last_label} 監測報告數 {last_count}，前 3 月平均 {prior_avg:.0f}（下滑 {drop_pct:.0f}%）。\n"
        f"前 3 月：{prior_detail}\n"
        f"可能原因：工地完工退場、法規執行面變動、合規鬆動、新建案資料源切換。\n"
        f"建議：抽樣 3-5 個前月活躍但本月沒動的工地，檢查 Drive 資料夾。"
    )
    print(f"\n⚠️  月度監測活動異常下滑\n{msg}")

    if notify:
        try:
            from notify import send_notification
            send_notification(
                f'⚠️ {last_label} 監測報告下滑 {drop_pct:.0f}%',
                msg,
                use_clickup=True,
            )
        except Exception as e:
            print(f"  通知發送失敗: {e}")

    state['last_alerted_month'] = last_label
    try:
        with open(MONTHLY_ALERT_STATE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"  ⚠️ 無法寫入告警狀態: {e}")


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

    # 月度活動趨勢檢查
    check_monthly_activity_trend(notify=args.notify)

    # 回傳 diff 供週報使用
    return diff


if __name__ == '__main__':
    main()
