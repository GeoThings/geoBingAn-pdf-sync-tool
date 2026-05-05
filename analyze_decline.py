#!/usr/bin/env python3
"""偵測「前 N 月活躍但目標月歸零」的工地，產出可立即調查的候選清單。

掛在 weekly_snapshot.check_monthly_activity_trend 後面：
月度告警觸發時，自動附帶 top 候選 + Drive folder URL，
不用人再手寫 query。

也可獨立 CLI 跑：
    python3 analyze_decline.py                 # 對上一個完整月份分析
    python3 analyze_decline.py --month 2026-04 # 指定月份
    python3 analyze_decline.py --top 10        # 顯示前 10 名
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DRIVE_CACHE_FILE = Path(__file__).parent / 'state' / 'uploaded_to_geobingan_7days.json'

# 真工地名稱必含的關鍵字（過濾「2026年03月」、「115年1月」這類時間分類資料夾）
SITE_NAME_KEYWORDS = re.compile(r'(建字|工程|新建|工地|大樓|社區|觀測|監測|案|場)')


def _months_back(year: int, month: int, n: int) -> Tuple[int, int]:
    month -= n
    while month < 1:
        month += 12
        year -= 1
    return year, month


def _is_real_site(folder_name: str) -> bool:
    """過濾時間分類資料夾（如 `2026年03月`、`115年1月`、`11502`）。"""
    if not folder_name:
        return False
    # 純數字或數字+年/月結尾，視為時間資料夾
    if re.fullmatch(r'\d{4,6}|11\d{0,2}年?\d{0,2}月?|2026年?\d{1,2}月?|115\.\d{1,2}|115年?', folder_name):
        return False
    return bool(SITE_NAME_KEYWORDS.search(folder_name))


def _parse_filename_date(name: str, folder: str = ''):
    """共用 parser：先檔名，再 folder + 檔名 fallback。"""
    from filename_date_parser import parse_date_from_filename
    d = parse_date_from_filename(name)
    if d is None and folder:
        d = parse_date_from_filename(folder + '/' + name)
    return d


def find_decline_candidates(
    pdfs: List[dict],
    target_year: int,
    target_month: int,
    *,
    prior_months: int = 3,
    min_prior_per_month: int = 2,
    max_target: int = 0,
    top: int = 5,
) -> List[dict]:
    """找出前 prior_months 月每月 >= min_prior_per_month 但目標月 <= max_target 的工地。

    回傳每個候選含：folder, folder_id, latest_filename, latest_filename_date,
    latest_drive_modified_time, batch_upload_warning, monthly_distribution。
    """
    by_folder = defaultdict(lambda: defaultdict(int))
    folder_id_lookup: Dict[str, str] = {}
    folder_files: Dict[str, list] = defaultdict(list)
    folder_modified_times: Dict[str, set] = defaultdict(set)

    for p in pdfs:
        folder = p.get('folder_name', '')
        if not folder:
            continue
        folder_id_lookup[folder] = p.get('folder_id', '')
        modified = p.get('modifiedTime', '')
        if modified:
            folder_modified_times[folder].add(modified[:10])

        d = _parse_filename_date(p.get('name', ''), folder)
        if d is None:
            continue
        by_folder[folder][(d.year, d.month)] += 1
        folder_files[folder].append((d, p.get('name', ''), modified))

    prior_keys = [_months_back(target_year, target_month, n) for n in range(1, prior_months + 1)]

    candidates = []
    for folder, months in by_folder.items():
        if not _is_real_site(folder):
            continue
        prior_counts = [months.get(k, 0) for k in prior_keys]
        if not all(c >= min_prior_per_month for c in prior_counts):
            continue
        if months.get((target_year, target_month), 0) > max_target:
            continue

        files = sorted(folder_files[folder], key=lambda x: x[0], reverse=True)
        latest = files[0] if files else None

        # batch upload 偵測：所有 modifiedTime 集中在 1-2 天內 + 檔名日期 > 6 個月舊
        mt_dates = folder_modified_times[folder]
        batch_warn = ''
        if len(mt_dates) <= 2 and latest:
            latest_age_days = (datetime.now() - latest[0]).days
            if latest_age_days > 180:
                batch_warn = f'⚠️ 疑似批次回填（modifiedTime 集中、檔名日期 {latest_age_days}d 前）'

        prior_total = sum(prior_counts)
        candidates.append({
            'folder': folder,
            'folder_id': folder_id_lookup.get(folder, ''),
            'prior_total': prior_total,
            'monthly': {f'{y}-{m:02d}': months.get((y, m), 0) for y, m in
                        [_months_back(target_year, target_month, n) for n in range(prior_months, -2, -1)]},
            'latest_filename': latest[1] if latest else None,
            'latest_filename_date': latest[0].date().isoformat() if latest else None,
            'latest_modified': latest[2][:10] if latest and latest[2] else None,
            'batch_upload_warning': batch_warn,
        })

    candidates.sort(key=lambda c: -c['prior_total'])
    return candidates[:top]


def format_candidates(candidates: List[dict], target_label: str) -> str:
    """格式化成 ClickUp comment / log 用的純文字。"""
    if not candidates:
        return '（無符合條件的候選 — 衰退可能分散在多工地、無單一明顯停報）'
    lines = [f'🎯 {target_label} 候選工地（前月活躍、本月歸零）：']
    for i, c in enumerate(candidates, 1):
        lines.append('')
        lines.append(f'{i}. {c["folder"]}')
        if c['folder_id']:
            lines.append(f'   https://drive.google.com/drive/folders/{c["folder_id"]}')
        dist = ' '.join(f'{m}={n}' for m, n in c['monthly'].items())
        lines.append(f'   月分布: {dist}')
        if c['latest_filename']:
            lines.append(f'   最新檔: {c["latest_filename"][:60]} (檔名日期 {c["latest_filename_date"]}, Drive {c["latest_modified"]})')
        if c['batch_upload_warning']:
            lines.append(f'   {c["batch_upload_warning"]}')
    return '\n'.join(lines)


def load_pdfs() -> Optional[list]:
    if not DRIVE_CACHE_FILE.exists():
        return None
    try:
        with open(DRIVE_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f).get('cache', {}).get('pdfs', [])
    except (json.JSONDecodeError, IOError):
        return None


def main():
    parser = argparse.ArgumentParser(description='偵測前月活躍但目標月歸零的工地候選')
    parser.add_argument('--month', help='目標月份 YYYY-MM（預設為上一個完整月）')
    parser.add_argument('--top', type=int, default=5)
    parser.add_argument('--prior', type=int, default=3, help='往前看幾個月作為基準')
    parser.add_argument('--min-prior', type=int, default=2, help='前每月最少報告數')
    args = parser.parse_args()

    if args.month:
        ty, tm = map(int, args.month.split('-'))
    else:
        today = datetime.now()
        ty, tm = _months_back(today.year, today.month, 1)

    pdfs = load_pdfs()
    if pdfs is None:
        print('❌ 找不到或無法讀取 cache')
        sys.exit(1)

    candidates = find_decline_candidates(
        pdfs, ty, tm,
        prior_months=args.prior,
        min_prior_per_month=args.min_prior,
        top=args.top,
    )
    print(format_candidates(candidates, f'{ty}-{tm:02d}'))


if __name__ == '__main__':
    main()
