#!/usr/bin/env python3
"""
建案監測週報產生器

用法：
  python3 generate_weekly_report.py --type sync      # 週一同步後
  python3 generate_weekly_report.py --type summary    # 週五總結
  python3 generate_weekly_report.py --upload           # 產生後上傳到 ClickUp
"""

import json
import os
import re
import sys
import argparse
import requests
from datetime import datetime, timedelta
from pathlib import Path

# ClickUp
CLICKUP_TOKEN = os.environ.get('CLICKUP_TOKEN', 'pk_48123565_3YBBPG98F9LB9VM694I2V0IOLYIOAPJ0')
WEEKLY_REPORT_TASK_ID = '86ex8u782'

STATE_DIR = './state'


def load_data():
    """載入所有需要的資料"""
    with open(f'{STATE_DIR}/permit_system_mapping.json', 'r', encoding='utf-8') as f:
        mapping = json.load(f)
    with open(f'{STATE_DIR}/permit_registry.json', 'r', encoding='utf-8') as f:
        registry = json.load(f)
    return mapping, registry


def gather_stats(mapping, registry, days=7):
    """彙整統計資料"""
    now = datetime.now()
    cutoff = now - timedelta(days=days)
    cutoff_str = cutoff.strftime('%Y-%m-%d')

    total = len(mapping)
    total_pdfs = sum(d.get('drive_count', 0) for d in mapping.values())
    total_ai = sum(d.get('system_count', 0) for d in mapping.values())

    # 狀態分布
    statuses = {}
    for d in mapping.values():
        s = d.get('status', '')
        statuses[s] = statuses.get(s, 0) + 1

    # 本週更新
    updated = []
    for p, d in mapping.items():
        latest = d.get('latest_report', '')
        if latest and latest[:10] >= cutoff_str:
            info = registry.get(p, {})
            updated.append({
                'permit': p,
                'name': info.get('name', '') or '-',
                'drive': d.get('drive_count', 0),
                'ai': d.get('system_count', 0),
                'latest': latest[:10],
            })
    updated.sort(key=lambda x: x['latest'], reverse=True)

    # 警戒值（只顯示有 AI 辨識的）
    danger_alerts = []
    warning_alerts = []
    for p, info in registry.items():
        la = info.get('live_alerts', {})
        sys_count = mapping.get(p, {}).get('system_count', 0)
        if la and la.get('total', 0) > 0 and sys_count > 0:
            entry = {
                'permit': p,
                'name': info.get('name', '')[:25] or '-',
                'danger': la.get('danger', 0),
                'warning': la.get('warning', 0),
                'date': la.get('latest_date', ''),
                'details': la.get('details', []),
            }
            if la.get('danger', 0) > 0:
                danger_alerts.append(entry)
            else:
                warning_alerts.append(entry)

    danger_alerts.sort(key=lambda x: x['date'], reverse=True)
    warning_alerts.sort(key=lambda x: x['date'], reverse=True)

    return {
        'total': total,
        'total_pdfs': total_pdfs,
        'total_ai': total_ai,
        'statuses': statuses,
        'updated': updated,
        'danger_alerts': danger_alerts,
        'warning_alerts': warning_alerts,
        'report_date': now.strftime('%Y-%m-%d'),
        'period_start': cutoff.strftime('%Y/%m/%d'),
        'period_end': now.strftime('%Y/%m/%d'),
        'trend': None,  # 由 main() 填入
    }


def generate_html(stats, report_type='summary'):
    """產生週報 HTML"""
    s = stats
    title = '建案監測同步報告' if report_type == 'sync' else '建案監測週報'
    
    # 狀態名稱（與 web 版一致）
    status_labels = {
        'completed': ('已完成', '#22c55e', 'AI 已辨識全部報告'),
        'in_progress': ('部分對應', '#3b82f6', '部分報告已對應到 AI 分析'),
        'not_uploaded': ('待上傳', '#f59e0b', '雲端有報告，AI 尚未對應'),
        'completed_project': ('已結案', '#9ca3af', '建照年份 110 年前且無報告'),
        'no_reports': ('無資料', '#d1d5db', '尚無雲端報告'),
    }

    def esc(text):
        return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    html = f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<title>{title} {s["period_end"]}</title>
<style>
@page {{ size: A4; margin: 18mm 15mm; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
@font-face {{ font-family: 'LocalChinese'; src: url('file:///Library/Fonts/Arial Unicode.ttf'); }}
body {{ font-family: 'LocalChinese', sans-serif; color: #1a1a1a; line-height: 1.55; font-size: 11px; -webkit-print-color-adjust: exact; print-color-adjust: exact; }}

.header {{ background: linear-gradient(135deg, #1a1a1a 0%, #2d2d2d 100%); color: white; padding: 28px 36px; }}
.header h1 {{ font-size: 20px; font-weight: 700; }}
.header .subtitle {{ font-size: 12px; color: #aaa; margin-top: 2px; }}
.header .period {{ font-size: 13px; color: #e74c3c; font-weight: 600; margin-top: 6px; }}

.content {{ padding: 20px 36px; }}

.stats-row {{ display: flex; gap: 10px; margin-bottom: 20px; }}
.stat-card {{ flex: 1; background: #f8f9fa; border-radius: 6px; padding: 14px 10px; text-align: center; border: 1px solid #e9ecef; }}
.stat-card .num {{ font-size: 20px; font-weight: 700; }}
.stat-card .label {{ font-size: 9px; color: #6b7280; margin-top: 2px; }}
.stat-card.danger {{ border-left: 3px solid #dc2626; }}
.stat-card.warning {{ border-left: 3px solid #f59e0b; }}

h2 {{ font-size: 14px; font-weight: 700; margin: 20px 0 8px; padding-bottom: 4px; border-bottom: 2px solid #e5e7eb; color: #1a1a1a; }}

table {{ width: 100%; border-collapse: collapse; font-size: 10px; margin-bottom: 14px; }}
th {{ background: #f3f4f6; padding: 6px 8px; text-align: left; font-weight: 600; border-bottom: 2px solid #d1d5db; white-space: nowrap; }}
td {{ padding: 6px 8px; border-bottom: 1px solid #d1d5db; white-space: nowrap; }}
td.name {{ white-space: normal; word-break: keep-all; max-width: 160px; }}
tr {{ height: 28px; page-break-inside: avoid; }}
tr:nth-child(even) {{ background: #fafafa; }}
.num {{ text-align: right; min-width: 30px; }}
.empty {{ color: #999; }}

.badge {{ display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; white-space: nowrap; }}
.badge-danger {{ background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }}
.badge-warning {{ background: #fffbeb; color: #d97706; border: 1px solid #fde68a; }}
.badge-success {{ background: #f0fdf4; color: #16a34a; border: 1px solid #bbf7d0; }}
.badge-info {{ background: #eff6ff; color: #2563eb; border: 1px solid #bfdbfe; }}
.badge-gray {{ background: #f3f4f6; color: #6b7280; border: 1px solid #d1d5db; }}


.section-box {{ border-radius: 6px; padding: 14px; margin-bottom: 14px; page-break-inside: avoid; }}
.section-danger {{ background: #fef2f2; border: 1px solid #fecaca; }}
.section-warning {{ background: #fffbeb; border: 1px solid #fde68a; }}
.section-box h3 {{ font-size: 13px; margin-bottom: 8px; }}

.status-bar {{ width: 100%; height: 20px; border-radius: 4px; overflow: hidden; margin: 6px 0 14px; border-collapse: collapse; }}
.status-bar td {{ text-align: center; font-size: 9px; color: white; font-weight: 600; padding: 2px 4px; white-space: nowrap; }}

.footer {{ text-align: center; color: #9ca3af; font-size: 10px; padding: 16px; border-top: 1px solid #e5e7eb; margin-top: 20px; }}

.empty {{ color: #9ca3af; }}
.nowrap {{ white-space: nowrap; }}
</style>
</head>
<body>

<div class="header">
<h1>{title}</h1>
<div class="subtitle">geoBingAn 究平安 ・ 建案監測追蹤</div>
<div class="period">📅 {s["period_start"]}（{'一二三四五六日'[datetime.strptime(s['period_start'].replace('/','-'), '%Y-%m-%d').weekday()]}）— {s["period_end"]}（{'一二三四五六日'[datetime.strptime(s['period_end'].replace('/','-'), '%Y-%m-%d').weekday()]}）</div>
</div>

<div class="content">

<div class="stats-row">
<div class="stat-card"><div class="num">{s["total"]}</div><div class="label">追蹤建案</div></div>
<div class="stat-card"><div class="num">{s["total_pdfs"]:,}</div><div class="label">雲端報告</div></div>
<div class="stat-card"><div class="num">{s["total_ai"]:,}</div><div class="label">AI 已分析</div></div>
<div class="stat-card"><div class="num">{len(s["updated"])}</div><div class="label">本週更新</div></div>
<div class="stat-card danger"><div class="num">{len(s["danger_alerts"])}</div><div class="label">🔴 行動值</div></div>
<div class="stat-card warning"><div class="num">{len(s["warning_alerts"])}</div><div class="label">⚠️ 警戒值</div></div>
</div>
'''

    # Trend section (if available)
    trend = s.get('trend')
    if trend:
        def arrow(n):
            if n > 0: return f'<span style="color:#16a34a">+{n}</span>'
            if n < 0: return f'<span style="color:#dc2626">{n}</span>'
            return '<span style="color:#6b7280">0</span>'

        html += '<table style="font-size:10px;margin-bottom:16px"><tr>'
        html += f'<td style="border:none;padding:4px 12px">建案 {arrow(trend["total_change"])}</td>'
        html += f'<td style="border:none;padding:4px 12px">雲端 {arrow(trend["pdfs_change"])}</td>'
        html += f'<td style="border:none;padding:4px 12px">AI {arrow(trend["ai_change"])}</td>'
        html += f'<td style="border:none;padding:4px 12px">警戒 {arrow(trend["alerts_change"])}</td>'
        html += '</tr></table>'

        if trend.get('new_permits'):
            html += f'<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;padding:10px;margin-bottom:14px;font-size:10px">'
            html += f'<strong>🆕 新增建案（{len(trend["new_permits"])} 個）：</strong> '
            html += '、'.join(esc(p) for p in trend['new_permits'][:8])
            if len(trend['new_permits']) > 8:
                html += f'...等 {len(trend["new_permits"])} 個'
            html += '</div>'

    html += '''
<h2>📋 建案狀態分布</h2>
<table class="status-bar"><tr>'''

    for status_key in ['completed', 'in_progress', 'not_uploaded', 'completed_project', 'no_reports']:
        count = s['statuses'].get(status_key, 0)
        label, color, _ = status_labels.get(status_key, ('', '#ccc', ''))
        pct = count / s['total'] * 100 if s['total'] > 0 else 0
        short_labels = {'completed': '完成', 'in_progress': '部分', 'not_uploaded': '待傳', 'completed_project': '結案', 'no_reports': ''}
        short = short_labels.get(status_key, '')
        html += f'<td style="width:{pct:.0f}%;background:{color}">{short}{count}</td>'

    html += '</tr></table><table><tr><th>狀態</th><th>數量</th><th>占比</th><th>說明</th></tr>'
    for status_key in ['completed', 'in_progress', 'not_uploaded', 'completed_project', 'no_reports']:
        count = s['statuses'].get(status_key, 0)
        label, _, desc = status_labels.get(status_key, ('', '', ''))
        badge_cls = {'completed': 'success', 'in_progress': 'info', 'not_uploaded': 'warning'}.get(status_key, 'gray')
        pct = count / s['total'] * 100 if s['total'] > 0 else 0
        html += f'<tr><td><span class="badge badge-{badge_cls}">{esc(label)}</span></td><td>{count}</td><td>{pct:.0f}%</td><td>{esc(desc)}</td></tr>'
    html += '</table>'

    # Danger alerts
    if s['danger_alerts']:
        html += '<h2 style="color:#dc2626">🔴 行動值超標建案 — 需立即評估</h2>\n<table>\n'
        html += '<tr><th>建照號碼</th><th>工地名稱</th><th>觸發日期</th><th>異常說明</th></tr>\n'
        for a in s['danger_alerts']:
            detail = '、'.join(a['details']) if a['details'] else '有感測器超過行動值'
            a_name = a['name'] if a['name'] and a['name'] != '-' else '（未命名）'
            html += f'<tr><td>{esc(a["permit"])}</td><td class="name">{esc(a_name)}</td><td>{esc(a["date"])}</td><td>{esc(detail)}</td></tr>\n'
        html += '</table>\n'

    # Warning alerts
    if s['warning_alerts']:
        html += '<h2 style="color:#d97706">⚠️ 警戒值超標建案 — 持續監控</h2>\n<table>\n'
        html += '<tr><th>建照號碼</th><th>工地名稱</th><th>觸發日期</th></tr>\n'
        for a in s['warning_alerts']:
            a_name = a['name'] if a['name'] and a['name'] != '-' else '（未命名）'
            html += f'<tr><td>{esc(a["permit"])}</td><td class="name">{esc(a_name)}</td><td>{esc(a["date"])}</td></tr>\n'
        html += '</table>\n'

    # Updated permits
    html += '<h2>🔄 本週更新建案</h2><table>'
    html += '<tr><th style="width:24px">#</th><th>建照號碼</th><th>工地名稱</th><th class="num">雲端數</th><th class="num">AI數</th><th class="num">進度</th><th>更新日期</th></tr>'
    for i, u in enumerate(s['updated'], 1):
        pct = min(100, int(u['ai'] / u['drive'] * 100)) if u['drive'] > 0 and u['ai'] > 0 else 0
        pct_text = f'{pct}%' if pct > 0 else '0%'
        ai_text = str(u['ai']) if u['ai'] > 0 else '0'
        name_raw = u['name'] if u['name'] and u['name'] != '-' else '（未命名）'
        name_display = esc(name_raw[:20] + '...' if len(name_raw) > 20 else name_raw)
        html += (
            f'<tr>'
            f'<td>{i}</td>'
            f'<td>{esc(u["permit"])}</td>'
            f'<td class="name">{name_display}</td>'
            f'<td class="num">{u["drive"]}</td>'
            f'<td class="num">{ai_text}</td>'
            f'<td class="num">{pct_text}</td>'
            f'<td>{u["latest"]}</td>'
            f'</tr>\n'
        )
    html += '</table>'

    html += f'''
<div class="footer">
geoBingAn 究平安 ・ 建案監測週報 ・ {s["report_date"]} 自動產生
</div>
</div>
</body>
</html>'''

    return html


def html_to_pdf(html_content, output_path):
    """HTML 轉 PDF（使用 Chrome headless）"""
    import subprocess
    import tempfile

    html_path = tempfile.mktemp(suffix='.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
    if os.path.exists(chrome):
        subprocess.run([
            chrome, '--headless', '--disable-gpu', '--no-sandbox',
            f'--print-to-pdf={output_path}',
            '--print-to-pdf-no-header',
            html_path
        ], capture_output=True, timeout=30)
        os.unlink(html_path)
        if os.path.exists(output_path):
            size_kb = os.path.getsize(output_path) / 1024
            print(f"  PDF 產生完成: {output_path} ({size_kb:.0f} KB)")
            return

    # Fallback to weasyprint
    from weasyprint import HTML
    HTML(filename=html_path).write_pdf(output_path)
    if os.path.exists(html_path):
        os.unlink(html_path)
    size_kb = os.path.getsize(output_path) / 1024
    print(f"  PDF 產生完成（weasyprint fallback）: {output_path} ({size_kb:.0f} KB)")


def upload_to_clickup(pdf_path, summary_text):
    """上傳 PDF 到 ClickUp task comment"""
    headers = {'Authorization': CLICKUP_TOKEN}

    # 上傳附件
    filename = os.path.basename(pdf_path)
    with open(pdf_path, 'rb') as f:
        r = requests.post(
            f'https://api.clickup.com/api/v2/task/{WEEKLY_REPORT_TASK_ID}/attachment',
            headers=headers,
            files={'attachment': (filename, f, 'application/pdf')}
        )
    if r.status_code == 200:
        print(f"  附件已上傳: {filename}")
    else:
        print(f"  附件上傳失敗: {r.status_code} {r.text[:200]}")

    # 發送 comment
    r2 = requests.post(
        f'https://api.clickup.com/api/v2/task/{WEEKLY_REPORT_TASK_ID}/comment',
        headers={**headers, 'Content-Type': 'application/json'},
        json={'comment_text': summary_text}
    )
    if r2.status_code == 200:
        print(f"  Comment 已發送")
    else:
        print(f"  Comment 發送失敗: {r2.status_code}")


def main():
    parser = argparse.ArgumentParser(description='建案監測週報產生器')
    parser.add_argument('--type', choices=['sync', 'summary'], default='summary',
                        help='sync=週一同步報告, summary=週五總結週報')
    parser.add_argument('--upload', action='store_true', help='產生後上傳到 ClickUp')
    parser.add_argument('--days', type=int, default=7, help='回溯天數（預設 7）')
    args = parser.parse_args()

    print("=" * 50)
    print(f"📊 建案監測{'同步報告' if args.type == 'sync' else '週報'}產生器")
    print("=" * 50)

    # 載入資料
    mapping, registry = load_data()
    stats = gather_stats(mapping, registry, days=args.days)

    # 儲存快照並取得趨勢
    try:
        from weekly_snapshot import save_snapshot, get_previous_snapshot, compute_diff
        current_snap = save_snapshot()
        previous_snap = get_previous_snapshot()
        trend = compute_diff(current_snap, previous_snap)
        stats['trend'] = trend
        if trend:
            print(f"  趨勢：建案{'+' if trend['total_change']>=0 else ''}{trend['total_change']}、"
                  f"AI{'+' if trend['ai_change']>=0 else ''}{trend['ai_change']}、"
                  f"新增 {len(trend['new_permits'])} 個建案")
    except Exception as e:
        print(f"  快照/趨勢分析失敗: {e}")

    # 產生 HTML
    html = generate_html(stats, report_type=args.type)

    # 轉 PDF
    date_str = stats['report_date'].replace('-', '')
    type_label = '同步報告' if args.type == 'sync' else '週報'
    pdf_filename = f'建案監測{type_label}_{stats["period_start"].replace("/","-")}_{stats["period_end"].replace("/","-")}.pdf'
    pdf_path = f'{STATE_DIR}/{pdf_filename}'

    html_to_pdf(html, pdf_path)

    # 上傳到 ClickUp
    if args.upload:
        emoji = '🔄' if args.type == 'sync' else '📊'
        summary = (
            f'{emoji} {"同步報告" if args.type == "sync" else "週報"} '
            f'{stats["period_start"]} — {stats["period_end"]}\n\n'
            f'• 追蹤建案：{stats["total"]} 個\n'
            f'• 雲端報告：{stats["total_pdfs"]:,} 份\n'
            f'• AI 已分析：{stats["total_ai"]:,} 份\n'
            f'• 本週更新：{len(stats["updated"])} 個建案\n'
            f'• 🔴 行動值超標：{len(stats["danger_alerts"])} 個\n'
            f'• ⚠️ 警戒值超標：{len(stats["warning_alerts"])} 個\n\n'
            f'詳見附件 PDF。'
        )
        upload_to_clickup(pdf_path, summary)

    print(f"\n✅ 完成！")


if __name__ == '__main__':
    main()
