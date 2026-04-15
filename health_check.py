#!/usr/bin/env python3
"""
系統健康檢查腳本

每日執行，檢查：
1. JWT Token 有效期
2. 磁碟空間
3. 最近一次同步狀態
4. API 可用性

異常時發送通知。

用法：
  python3 health_check.py          # 檢查並顯示結果
  python3 health_check.py --notify  # 異常時發送通知
"""

import os
import sys
import json
import time
import argparse
import shutil
from datetime import datetime
from pathlib import Path

# 載入設定
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()


def check_token():
    """檢查 JWT Token 有效期"""
    from jwt_auth import decode_jwt_payload
    refresh = os.getenv('REFRESH_TOKEN', '')
    if not refresh:
        return 'error', 'REFRESH_TOKEN 未設定'

    try:
        payload = decode_jwt_payload(refresh)
        exp = payload.get('exp', 0)
        days_left = (exp - time.time()) / 86400
        if days_left < 0:
            return 'error', f'Refresh Token 已過期 {-days_left:.1f} 天'
        elif days_left < 2:
            return 'warning', f'Refresh Token 剩餘 {days_left:.1f} 天'
        else:
            return 'ok', f'Refresh Token 剩餘 {days_left:.1f} 天'
    except Exception as e:
        return 'error', f'Token 檢查失敗: {e}'


def check_disk():
    """檢查磁碟空間"""
    usage = shutil.disk_usage('/')
    free_gb = usage.free / (1024**3)
    pct_used = usage.used / usage.total * 100
    if free_gb < 5:
        return 'warning', f'磁碟空間不足: {free_gb:.1f} GB 可用 ({pct_used:.0f}% 已用)'
    return 'ok', f'{free_gb:.1f} GB 可用'


def check_last_sync():
    """檢查最近一次同步狀態"""
    status_file = './state/sync_status.json'
    if not os.path.exists(status_file):
        return 'warning', '找不到同步狀態檔案'

    try:
        with open(status_file, 'r') as f:
            data = json.load(f)
        last_run = data.get('last_run', '')
        last_status = data.get('last_status', '')
        if not last_run:
            return 'warning', '尚未執行過同步'

        days_ago = (datetime.now() - datetime.fromisoformat(last_run)).days
        if days_ago > 10:
            return 'warning', f'距離上次同步已 {days_ago} 天（{last_run[:10]}）'
        elif last_status == 'failure':
            return 'warning', f'上次同步失敗（{last_run[:10]}）'
        return 'ok', f'上次同步: {last_run[:10]}（{last_status}）'
    except Exception as e:
        return 'warning', f'讀取狀態失敗: {e}'


def check_api():
    """檢查 API 可用性"""
    import requests
    try:
        r = requests.get('https://riskmap.today/api/', timeout=10)
        if r.status_code < 500:
            return 'ok', f'API 正常（{r.status_code}）'
        return 'warning', f'API 回應異常（{r.status_code}）'
    except Exception as e:
        return 'error', f'API 無法連線: {e}'


def main():
    parser = argparse.ArgumentParser(description='系統健康檢查')
    parser.add_argument('--notify', action='store_true', help='異常時發送通知')
    args = parser.parse_args()

    checks = [
        ('JWT Token', check_token),
        ('磁碟空間', check_disk),
        ('同步狀態', check_last_sync),
        ('API 連線', check_api),
    ]

    icons = {'ok': '✅', 'warning': '⚠️', 'error': '❌'}
    issues = []

    print(f"🏥 系統健康檢查 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)
    for name, check_fn in checks:
        try:
            status, message = check_fn()
        except Exception as e:
            status, message = 'error', str(e)
        icon = icons.get(status, '❓')
        print(f"  {icon} {name}: {message}")
        if status != 'ok':
            issues.append(f'{icon} {name}: {message}')

    print("=" * 50)
    if issues:
        print(f"⚠️  {len(issues)} 個問題需要關注")
        if args.notify:
            try:
                from notify import send_notification
                send_notification(
                    f'⚠️ geoBingAn 健康檢查: {len(issues)} 個問題',
                    '\n'.join(issues)
                )
                print("  通知已發送")
            except Exception as e:
                print(f"  通知發送失敗: {e}")
    else:
        print("✅ 所有檢查通過")


if __name__ == '__main__':
    main()
