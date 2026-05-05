"""
建照監測追蹤報告 — HTML/CSV 報告生成模板

從 generate_permit_tracking_report.py 提取，降低主檔案複雜度。
"""
import os
import re
from datetime import datetime
from typing import Dict, List


def generate_html_report(permit_data: Dict[str, dict], non_google: List[dict], alert_data: Dict[str, dict] = None, permit_names: Dict[str, str] = None, output_path: str = None, gov_url_statuses: Dict[str, str] = None):
    """生成 HTML 報告"""
    import html as html_mod
    print("\n📊 生成 HTML 報告...")

    if alert_data is None:
        alert_data = {}
    if permit_names is None:
        permit_names = {}
    if gov_url_statuses is None:
        gov_url_statuses = {}

    def esc(s: str) -> str:
        """Escape string for safe HTML insertion (text and attributes)"""
        return html_mod.escape(str(s), quote=True) if s else ''

    now = datetime.now()

    # 統計
    total = len(permit_data)
    completed = sum(1 for p in permit_data.values() if p.get('status') == 'completed')
    in_progress = sum(1 for p in permit_data.values() if p.get('status') == 'in_progress')
    not_uploaded = sum(1 for p in permit_data.values() if p.get('status') == 'not_uploaded')
    no_reports = sum(1 for p in permit_data.values() if p.get('status') == 'no_reports')
    completed_project = sum(1 for p in permit_data.values() if p.get('status') == 'completed_project')
    other_cloud = len(non_google)
    errors = sum(1 for p in permit_data.values() if p.get('status') == 'error')

    # 建立非 Google 雲端服務分類
    cloud_groups = {}
    for item in non_google:
        cloud = item['cloud']
        if cloud not in cloud_groups:
            cloud_groups[cloud] = []
        cloud_groups[cloud].append(item['permit'])

    # 排序 (按數量)
    cloud_groups = dict(sorted(cloud_groups.items(), key=lambda x: -len(x[1])))

    # 雲端服務圖示
    cloud_icons = {
        'SharePoint': '📊', 'Dropbox': '📦', 'OneDrive': '☁️',
        'MEGA': '🔷', 'pCloud': '🌩️', 'GoFile': '📁',
        'ownCloud': '🔵', '短網址': '🔗'
    }

    # 生成雲端服務卡片
    cloud_cards_html = ""
    for cloud, permits in cloud_groups.items():
        icon = cloud_icons.get(cloud, '🌐')
        permits_html = ''.join([f'<li>{esc(p)}</li>' for p in permits[:20]])
        if len(permits) > 20:
            permits_html += f'<li>...還有 {len(permits) - 20} 個</li>'
        cloud_cards_html += f'''
<div class="cloud-card">
<h4><span class="icon">{icon}</span> {esc(cloud)} ({len(permits)})</h4>
<ul>{permits_html}</ul>
</div>'''

    # 建立非 Google 查詢表
    non_google_set = {item['permit']: item['cloud'] for item in non_google}

    # 需要關注：有警戒值的建案（只顯示有 AI 辨識紀錄的，避免誤配）
    alert_permits = []
    for permit_key, pdata in permit_data.items():
        if pdata.get('system_count', 0) == 0:
            continue  # AI 未對應的建案不顯示警戒值
        pa = alert_data.get(permit_key, {})
        if pa.get('total', 0) > 0:
            wc = pa.get('warning_count', 0)
            dc = pa.get('danger_count', 0)
            parts = []
            if wc > 0: parts.append(f'⚠️警戒值{wc}項')
            if dc > 0: parts.append(f'🔴行動值{dc}項')
            lad = pa.get('latest_alert_date', '')
            alert_permits.append({
                'permit': permit_key,
                'name': permit_names.get(permit_key, ''),
                'summary': ' '.join(parts),
                'latest_alert_date': lad[:10] if lad else '-',
                'details': pa.get('details', []),
            })

    # 需要關注：報告過期的建案 (days_since_update > 30 and status != 'no_reports')
    stale_permits = []
    for permit_key, pdata in permit_data.items():
        ds = pdata.get('days_since_update', '')
        st = pdata.get('status', '')
        if ds != '' and ds is not None and int(ds) > 30 and st != 'no_reports':
            stale_permits.append({
                'permit': permit_key,
                'name': permit_names.get(permit_key, ''),
                'days': int(ds),
                'latest': pdata.get('latest_report', '')[:10] if pdata.get('latest_report') else '-',
            })

    # HTML for 需要關注 cards
    attention_alert_cards = ""
    for ap in alert_permits:
        detail_text = ' / '.join(ap.get('details', [])) if ap.get('details') else ap['summary']
        attention_alert_cards += f'<div class="attention-card attention-card-alert"><div class="ac-permit">{esc(ap["permit"])}</div><div class="ac-name">{esc(ap["name"] or "-")}</div><div class="ac-summary">{esc(ap["summary"])}</div><div class="ac-detail">{esc(detail_text)}</div><div class="ac-date">最近觸發: {esc(ap["latest_alert_date"])}</div></div>'

    attention_stale_rows = ""
    for sp in stale_permits:
        attention_stale_rows += f'<tr><td><strong>{esc(sp["permit"])}</strong></td><td>{esc(sp["name"] or "-")}</td><td><span class="days days-old" data-date="{esc(sp["latest"])}"></span></td></tr>'

    has_attention = len(alert_permits) > 0 or len(stale_permits) > 0
    attention_open = 'open' if has_attention else ''

    # 排序：有更新的優先（最近更新日期降序），無更新的按建照號碼排
    def sort_key(permit):
        data = permit_data[permit]
        latest = data.get('latest_report', '') or ''
        # 有更新日期的排前面（日期越新越前）
        has_update = 1 if latest else 0
        # 建照號碼作為次要排序
        year = int(re.search(r'(\d{2,3})建字', permit).group(1)) if re.search(r'(\d{2,3})建字', permit) else 0
        num = int(re.search(r'第(\d+)號', permit).group(1)) if re.search(r'第(\d+)號', permit) else 0
        return (-has_update, latest if latest else '', -year, -num)
    sorted_permits = sorted(permit_data.keys(), key=sort_key, reverse=False)
    # 有更新的按日期降序（最新在前）
    sorted_permits.sort(key=lambda x: permit_data[x].get('latest_report', '') or '', reverse=True)

    # 生成表格行
    rows_html = ""
    for i, permit in enumerate(sorted_permits, 1):
        data = permit_data[permit]
        cloud = non_google_set.get(permit, 'Google Drive')
        drive_count = data.get('drive_count', 0)
        system_count = data.get('system_count', 0)
        status = data.get('status', 'unknown')
        latest = data.get('latest_report', '')
        days = data.get('days_since_update', '')
        folder_id = data.get('folder_id', '')

        # 狀態 badge
        status_badges = {
            'completed': ('✔ 已完成', 'badge-success'),
            'in_progress': ('⏳ 部分對應', 'badge-info'),
            'not_uploaded': ('⬆ 待上傳', 'badge-warning'),
            'no_reports': ('── 無資料', 'badge-gray'),
            'completed_project': ('🏁 已結案', 'badge-gray'),
            'error': ('✖ 異常', 'badge-danger')
        }
        badge_text, badge_class = status_badges.get(status, ('未知', 'badge-gray'))

        # 雲端 badge
        if cloud == 'Google Drive':
            cloud_badge = ''
        else:
            cloud_badge = f'<span class="badge badge-orange">{esc(cloud)}</span>'

        # 覆蓋率
        if drive_count > 0 and system_count > 0:
            coverage = min(100, int(system_count / drive_count * 100))
            bar_color = '#22c55e' if coverage >= 80 else '#f59e0b' if coverage >= 50 else '#dc2626'
            coverage_html = f'<div class="progress-wrapper"><div class="progress-text">{coverage}%</div><div class="bar"><div class="bar-fill" style="width:{coverage}%;background:{bar_color}"></div></div></div>'
        else:
            coverage_html = '<span class="empty-val">-</span>'

        # 天數 - use data-date for dynamic JS calculation
        if latest:
            days_html = f'<span class="days" data-date="{latest[:10]}"></span>'
        else:
            days_html = '<span class="empty-val">-</span>'

        # 連結
        if folder_id:
            drive_link = f'<a href="https://drive.google.com/drive/folders/{folder_id}" target="_blank" title="開啟 Google Drive 資料夾">{drive_count} ↗</a>'
        else:
            drive_link = str(drive_count)

        # 最新報告日期
        latest_html = latest[:10] if latest else '<span class="empty-val">-</span>'

        # 建案名稱
        building_name = permit_names.get(permit, '')
        # 截斷過長的名稱
        if len(building_name) > 25:
            name_html = f'<span title="{esc(building_name)}">{esc(building_name[:25])}...</span>'
        else:
            name_html = esc(building_name) if building_name else '<span class="empty-val">-</span>'

        # 即時監測狀態（來自 construction-alerts API）
        # 只在有 AI 辨識紀錄的建案才顯示警戒值（AI=0 表示名稱匹配不可靠，警戒值可能也是誤配）
        permit_alert = alert_data.get(permit, {}) if system_count > 0 else {}
        alert_total = permit_alert.get('total', 0)
        warning_count = permit_alert.get('warning_count', 0)
        danger_count = permit_alert.get('danger_count', 0)
        latest_alert_date = permit_alert.get('latest_alert_date', '')
        alert_details = permit_alert.get('details', [])

        if alert_total > 0:
            # 顯示狀態 + 最近日期，讓使用者知道這是什麼時候的狀態
            alert_date_short = ''
            if latest_alert_date:
                # 顯示為 M/D 格式
                try:
                    parts = latest_alert_date[:10].split('-')
                    alert_date_short = f'{int(parts[1])}/{int(parts[2])}'
                except (IndexError, ValueError):
                    alert_date_short = latest_alert_date[:10]

            detail_tooltip = esc(' / '.join(alert_details)) if alert_details else ''
            status_parts = []
            if danger_count > 0:
                status_parts.append(f'🔴 行動值{danger_count}項')
            if warning_count > 0:
                status_parts.append(f'⚠️ 警戒值{warning_count}項')
            status_text = ' '.join(status_parts)
            date_label = f'<span class="alert-date">{alert_date_short}</span>' if alert_date_short else ''
            merged_alert_html = f'<span class="alert-merged" title="{detail_tooltip}">{status_text}{date_label}</span>'
        else:
            merged_alert_html = '<span class="empty-val">-</span>'

        # row CSS classes
        row_classes = []
        if alert_total > 0:
            row_classes.append('row-alert')
        # stale check done in JS via data-latest-date; add Python-side too for no_reports exclusion
        latest_date_attr = latest[:10] if latest else ''

        row_class_str = ' '.join(row_classes)

        # 政府 PDF folder URL 失效標記
        url_status = gov_url_statuses.get(permit, '')
        url_404_badge = ' <span class="badge badge-danger" title="政府 PDF folder URL 已失效，需聯繫建管處更新 PDF">URL 失效</span>' if url_status == '404' else ''

        rows_html += f'''
<tr data-status="{esc(status)}" data-cloud="{esc(cloud)}" data-alert-total="{alert_total}" data-latest-date="{esc(latest_date_attr)}" data-url-status="{esc(url_status)}" class="{row_class_str}">
<td>{i}</td>
<td><strong>{esc(permit)}</strong>{url_404_badge}</td>
<td class="name-cell">{name_html}</td>
<td>{cloud_badge}</td>
<td class="col-num">{drive_link}</td>
<td class="col-num">{system_count if system_count > 0 else ('<span class="empty-val">-</span>' if drive_count == 0 else '<span class="empty-val" title="PDF 已在雲端，尚未對應到 AI 分析結果">-</span>')}</td>
<td>{coverage_html}</td>
<td>{merged_alert_html}</td>
<td>{latest_html}</td>
<td class="col-num">{days_html}</td>
<td><span class="badge {badge_class}">{esc(badge_text)}</span></td>
</tr>'''

    html = f'''<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="utf-8"/>
<meta content="width=device-width, initial-scale=1.0" name="viewport"/>
<title>建照監測追蹤報告</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft JhengHei",sans-serif;background:#f3f4f6;padding:20px;color:#111827;line-height:1.5;font-size:13px;font-variant-numeric:tabular-nums}}
.container{{max-width:1600px;margin:0 auto;background:white;border-radius:12px;box-shadow:0 10px 25px -5px rgba(0,0,0,0.1),0 8px 10px -6px rgba(0,0,0,0.1);overflow:hidden}}
.header{{background:#0a0a0a;padding:24px 30px;display:flex;justify-content:space-between;align-items:center;border-top:4px solid #dc2626}}
.header h1{{font-size:22px;font-weight:900;color:#ffffff;letter-spacing:0.05em;display:flex;align-items:center;gap:12px}}
.header h1::before{{content:'';display:block;width:8px;height:24px;background:#dc2626;border-radius:2px}}
.header .meta{{font-size:12px;color:#9ca3af;font-weight:500;letter-spacing:0.05em}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;padding:20px 30px;background:#ffffff;border-bottom:1px solid #e5e7eb}}
.stat{{background:#f9fafb;padding:16px;border-radius:10px;border:1px solid #f3f4f6;transition:transform 0.2s,box-shadow 0.2s}}
.stat:hover{{transform:translateY(-2px);box-shadow:0 4px 6px -1px rgba(0,0,0,0.05);background:white}}
.stat .label{{font-size:11px;color:#6b7280;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px}}
.stat .value{{font-size:26px;font-weight:900;color:#111827}}
.legend-section{{background:white;border-bottom:1px solid #e5e7eb}}
.legend-toggle{{width:100%;background:none;border:none;padding:14px 30px;text-align:left;cursor:pointer;font-size:13px;font-weight:700;color:#111827;display:flex;align-items:center;gap:8px;transition:background 0.2s}}
.legend-toggle:hover{{background:#f9fafb;color:#dc2626}}
.toggle-arrow-legend{{transition:transform 0.2s;display:inline-block;font-size:10px;color:#9ca3af}}
.legend-section.open .toggle-arrow-legend{{transform:rotate(90deg)}}
.legend-body{{display:none;padding:0 30px 20px}}
.legend-section.open .legend-body{{display:block}}
.legend-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin-top:10px}}
.legend-block{{background:#f9fafb;border-radius:8px;padding:16px;border:1px solid #e5e7eb}}
.legend-block-title{{font-size:12px;font-weight:700;color:#111827;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid #e5e7eb}}
.legend-table{{width:100%;border-collapse:collapse;font-size:12px}}
.legend-table td{{padding:6px 0;vertical-align:top;color:#4b5563}}
.legend-col-name{{font-weight:700;color:#111827;width:100px}}
.attention-section{{background:white;border-bottom:1px solid #e5e7eb}}
.attention-toggle{{width:100%;background:#fff1f2;border:none;padding:14px 30px;text-align:left;cursor:pointer;font-size:13px;font-weight:700;color:#b91c1c;display:flex;align-items:center;gap:8px;transition:background 0.2s}}
.attention-toggle:hover{{background:#ffe4e6}}
.attention-toggle .toggle-arrow{{transition:transform 0.2s;display:inline-block;font-size:10px;color:#ef4444}}
.attention-section.open .toggle-arrow{{transform:rotate(90deg)}}
.attention-body{{display:none;padding:16px 30px 24px;background:#fff1f2}}
.attention-section.open .attention-body{{display:block}}
.attention-group{{margin-bottom:16px}}
.attention-group h4{{font-size:13px;color:#991b1b;margin-bottom:12px;font-weight:800}}
.attention-cards{{display:flex;flex-wrap:wrap;gap:10px}}
.attention-card{{background:white;border-radius:8px;padding:12px;min-width:180px;max-width:240px;border:1px solid #fecdd3;border-left:4px solid #ef4444;box-shadow:0 2px 4px rgba(220,38,38,0.05)}}
.ac-permit{{font-size:11px;font-weight:800;color:#b91c1c}}
.ac-name{{font-size:11px;color:#4b5563;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}}
.ac-summary{{font-size:13px;margin-top:8px;font-weight:700}}
.ac-detail{{font-size:11px;color:#6b7280;margin-top:4px}}
.ac-date{{font-size:10px;color:#9ca3af;margin-top:6px;font-weight:600}}
.stale-table{{width:100%;border-collapse:collapse;font-size:12px;background:white;border-radius:8px;overflow:hidden;border:1px solid #fecdd3}}
.stale-table th{{background:#ffe4e6;padding:10px 12px;text-align:left;font-size:11px;color:#991b1b;font-weight:700}}
.stale-table td{{padding:10px 12px;border-bottom:1px solid #fecdd3;color:#4b5563;font-weight:500}}
.non-google{{background:#fffbeb;padding:20px 30px;border-bottom:1px solid #e5e7eb}}
.non-google h3{{font-size:14px;color:#b45309;margin-bottom:16px;font-weight:800}}
.cloud-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}}
.cloud-card{{background:white;padding:16px;border-radius:8px;border:1px solid #fde68a;box-shadow:0 1px 2px rgba(0,0,0,0.02)}}
.cloud-card h4{{font-size:13px;color:#b45309;margin-bottom:10px;display:flex;align-items:center;gap:6px;border-bottom:1px solid #fef3c7;padding-bottom:8px;font-weight:800}}
.cloud-card .icon{{font-size:16px}}
.cloud-card ul{{font-size:11px;color:#4b5563;list-style:none;max-height:100px;overflow-y:auto;font-weight:500}}
.cloud-card li{{padding:4px 0;border-bottom:1px solid #f9fafb}}
.cloud-card li:last-child{{border-bottom:none}}
.content{{padding:24px 30px}}
.controls{{display:flex;flex-wrap:wrap;gap:16px;margin-bottom:20px;align-items:center;justify-content:flex-start}}
.filter-group{{display:flex;background:#f3f4f6;padding:4px;border-radius:8px;border:1px solid #e5e7eb}}
.filter-group .btn{{border:none;background:transparent;padding:8px 16px;font-size:12px;font-weight:600;color:#4b5563;border-radius:6px;cursor:pointer;transition:all 0.2s}}
.filter-group .btn:not(.active):hover{{color:#111827;background:#e5e7eb}}
.filter-group .btn.active{{background:#dc2626;color:white;box-shadow:0 2px 8px rgba(220,38,38,0.3)}}
.filter-group .btn.active:hover{{background:#b91c1c}}
.search{{padding:10px 16px;width:300px;border:1px solid #d1d5db;border-radius:8px;font-size:13px;background:#f9fafb;transition:all 0.2s;font-weight:500}}
.search:focus{{outline:none;border-color:#111827;background:white;box-shadow:0 0 0 3px rgba(17,24,39,0.1)}}
.table-wrap{{overflow-x:auto;border-radius:8px;max-height:800px;background:white}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
thead th{{background:#ffffff;color:#111827;font-weight:800;font-size:12px;position:sticky;top:0;z-index:10;border-bottom:2px solid #111827;padding:16px 12px;cursor:pointer;user-select:none;white-space:nowrap;transition:background 0.2s}}
thead th:hover{{background:#f9fafb;color:#dc2626}}
th.sort-asc::after{{content:' ↑';color:#dc2626;font-weight:900}}
th.sort-desc::after{{content:' ↓';color:#dc2626;font-weight:900}}
th:nth-child(1){{width:40px}}
td{{padding:14px 12px;border-bottom:1px solid #f3f4f6;vertical-align:middle;color:#374151;font-weight:500;white-space:nowrap}}
td.col-num,th.col-num{{text-align:right}}
.empty-val{{color:#d1d5db;font-weight:400;display:inline-block;text-align:center;width:100%}}
tr:hover td{{background:#f9fafb}}
tr{{transition:background 0.2s}}
tr.row-alert td:first-child{{border-left:4px solid #dc2626}}
tr.row-stale td:first-child{{border-left:4px solid #f59e0b}}
tr.row-alert.row-stale td:first-child{{border-left:4px solid #dc2626}}
.badge{{display:inline-flex;align-items:center;gap:4px;padding:4px 8px;border-radius:6px;font-size:11px;font-weight:700;white-space:nowrap;border:1px solid transparent}}
.badge-success{{background:#ecfdf5;color:#15803d;border-color:#a7f3d0}}
.badge-info{{background:#eff6ff;color:#1d4ed8;border-color:#bfdbfe}}
.badge-warning{{background:#fffbeb;color:#b45309;border-color:#fde68a}}
.badge-danger{{background:#fef2f2;color:#b91c1c;border-color:#fecdd3}}
.badge-gray{{background:#f9fafb;color:#6b7280;border-color:#e5e7eb}}
.badge-orange{{background:#fff7ed;color:#c2410c;border-color:#ffedd5}}
.progress-wrapper{{display:flex;align-items:center;gap:8px;min-width:100px}}
.progress-text{{width:36px;text-align:right;font-size:12px;color:#111827;font-weight:700}}
.bar{{flex-grow:1;height:6px;background:#e5e7eb;border-radius:4px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:4px;transition:width 0.5s ease-in-out}}
a{{color:#111827;text-decoration:none;font-weight:700;border-bottom:1px dashed #d1d5db;white-space:nowrap;display:inline-block;padding:6px;margin:-6px;border-radius:4px;transition:color 0.2s}}
a:hover{{color:#dc2626;border-bottom-color:#dc2626;background:#fff1f2}}
.days{{font-size:12px}}
.days-old{{color:#ffffff;font-weight:700;background:#ef4444;padding:4px 8px;border-radius:6px;box-shadow:0 1px 2px rgba(239,68,68,0.3);display:inline-block;white-space:nowrap;min-width:50px;text-align:center}}
.days-recent{{color:#10b981;font-weight:600}}
.alert-merged{{font-size:12px;white-space:nowrap;cursor:help;padding:2px 6px;background:#fff1f2;color:#b91c1c;border-radius:4px;border:1px solid #fecdd3;display:inline-flex;align-items:center;gap:6px;font-weight:700}}
.alert-date{{font-size:11px;color:#6b7280;font-weight:400;border-left:1px solid #fecdd3;padding-left:6px}}
.name-cell{{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:#111827;font-weight:600}}
@media (max-width:768px){{
.header{{flex-direction:column;align-items:flex-start;gap:10px;padding:15px}}
.stats{{grid-template-columns:1fr 1fr;padding:15px}}
.content{{padding:15px}}
.controls{{flex-direction:column;align-items:stretch}}
.search{{width:100%}}
.filter-group{{overflow-x:auto;flex-wrap:nowrap}}
}}
</style>
</head>
<body>
<div class="container">
<div class="header">
<div><h1>建照監測追蹤報告</h1></div>
<div class="meta">{now.strftime('%Y年%m月%d日 %H:%M')} | 自動生成</div>
</div>
<div class="stats">
<div class="stat" title="政府列管的建案監測數量"><div class="label">監測建案總數</div><div class="value">{total}</div></div>
<div class="stat" title="所有報告都已上傳到究平安系統完成分析"><div class="label">已完成上傳</div><div class="value" style="color:#22c55e">{completed}</div></div>
<div class="stat" title="部分報告已對應到 AI 分析結果"><div class="label">部分對應</div><div class="value" style="color:#3b82f6">{in_progress}</div></div>
<div class="stat" title="雲端有報告但尚未對應到 AI 分析"><div class="label">待上傳</div><div class="value" style="color:#f59e0b">{not_uploaded}</div></div>
<div class="stat" title="雲端資料夾中沒有任何 PDF 報告"><div class="label">尚無監測資料</div><div class="value" style="color:#6b7280">{no_reports}</div></div>
<div class="stat" title="最後更新超過一年，建案可能已完工"><div class="label">已結案</div><div class="value" style="color:#9ca3af">{completed_project}</div></div>
<div class="stat" title="使用 SharePoint、Dropbox 等其他雲端服務"><div class="label">非 Google Drive</div><div class="value" style="color:#c2410c">{other_cloud}</div></div>
<div class="stat" title="同步或上傳過程中發生錯誤"><div class="label">異常</div><div class="value" style="color:#dc2626">{errors}</div></div>
</div>

<div class="legend-section" id="legendSection">
<button class="legend-toggle" onclick="toggleLegend()">
<i class="toggle-arrow-legend">▶</i> 📖 圖例說明
</button>
<div class="legend-body" id="legendBody">
<div class="legend-grid">
<div class="legend-block">
<div class="legend-block-title">同步狀態說明</div>
<table class="legend-table">
<tr><td><span class="badge badge-success">已完成</span></td><td>所有雲端報告皆已上傳並完成分析</td></tr>
<tr><td><span class="badge badge-info">部分對應</span></td><td>部分報告已對應到 AI 分析結果，其餘因檔名無法自動匹配</td></tr>
<tr><td><span class="badge badge-warning">待上傳</span></td><td>雲端有報告但尚未對應到 AI 分析，可能尚未上傳或檔名無法匹配</td></tr>
<tr><td><span class="badge badge-gray">無資料</span></td><td>雲端資料夾目前沒有任何 PDF 報告</td></tr>
<tr><td><span class="badge badge-danger">異常</span></td><td>同步或上傳過程中發生錯誤，請聯絡技術人員</td></tr>
</table>
</div>
<div class="legend-block">
<div class="legend-block-title">列顏色說明</div>
<table class="legend-table">
<tr><td class="legend-color-cell" style="background:#fff1f2;border-left:3px solid #dc2626;">&nbsp;&nbsp;&nbsp;&nbsp;</td><td><strong>紅色底</strong>：該工地目前有監測警戒，需優先關注</td></tr>
<tr><td class="legend-color-cell" style="background:#fefce8;border-left:3px solid #f59e0b;">&nbsp;&nbsp;&nbsp;&nbsp;</td><td><strong>黃色底</strong>：報告超過 30 天未更新，請確認現場狀況</td></tr>
</table>
</div>
<div class="legend-block">
<div class="legend-block-title">欄位說明</div>
<table class="legend-table">
<tr><td class="legend-col-name">雲端報告數</td><td>Google Drive 上該工地的 PDF 報告總數（可點擊開啟資料夾）</td></tr>
<tr><td class="legend-col-name">AI 辨識數</td><td>已對應到此建案的 AI 分析報告數量。顯示 - 表示系統尚未對應（PDF 可能已上傳但檔名無法自動匹配）</td></tr>
<tr><td class="legend-col-name">系統處理進度</td><td>AI 辨識數 ÷ 雲端報告數，進度條顯示處理比例</td></tr>
<tr><td class="legend-col-name">監測警戒</td><td>即時監測狀態：⚠️ 警戒值（感測器超過警戒值）/ 🔴 行動值（超過行動值，需立即處理）。日期為最近一次觸發時間</td></tr>
<tr><td class="legend-col-name">更新間隔</td><td>最近一份報告距今天數，超過 30 天會以紅字標示</td></tr>
</table>
</div>
</div>
</div>
</div>

<div class="attention-section" id="attentionSection">
<button class="attention-toggle" onclick="toggleAttention()">
<i class="toggle-arrow">▶</i> ⚠️ 需要處理 — {len(alert_permits)} 個工地有監測警戒，{len(stale_permits)} 個工地報告超過 30 天未更新
</button>
<div class="attention-body">
<div class="attention-group">
<h4>目前有監測警戒的建案 ({len(alert_permits)} 個)</h4>
<div class="attention-cards">{attention_alert_cards if attention_alert_cards else '<span style="font-size:11px;color:#999">無</span>'}</div>
</div>
<div class="attention-group">
<h4>報告過期的建案 (超過 30 天未更新, {len(stale_permits)} 個)</h4>
<div style="max-height:300px;overflow-y:auto">
<table class="stale-table">
<thead><tr><th>建照字號</th><th>建案名稱</th><th>距今</th></tr></thead>
<tbody>{attention_stale_rows if attention_stale_rows else '<tr><td colspan="3" style="color:#999;font-size:11px;padding:6px">無</td></tr>'}</tbody>
</table>
</div>
</div>
</div>
</div>

<div class="non-google">
<h3>⚠️ 需手動處理的建照（未使用 Google Drive，系統無法自動抓取，共 {other_cloud} 個）</h3>
<div class="cloud-grid">{cloud_cards_html}</div>
</div>

<div class="content">
<div class="controls">
<input type="text" class="search" id="search" placeholder="搜尋建照號碼或建案名稱...">
<div class="filter-group">
<button class="btn active" onclick="filterStatus(this,'')">全部</button>
<button class="btn" onclick="filterStatus(this,'completed')">已完成</button>
<button class="btn" onclick="filterStatus(this,'in_progress')">部分對應</button>
<button class="btn" onclick="filterStatus(this,'not_uploaded')">待上傳</button>
<button class="btn" onclick="filterStatus(this,'other_cloud')">需手動處理</button>
<button class="btn" onclick="filterStatus(this,'needs_attention')">需要處理</button>
</div>
</div>
<div class="table-wrap">
<table id="dataTable">
<thead>
<tr>
<th onclick="sortTable(0)">#</th>
<th onclick="sortTable(1)">建照號碼</th>
<th onclick="sortTable(2)">工地名稱</th>
<th onclick="sortTable(3)" class="col-cloud">資料來源</th>
<th onclick="sortTable(4)" class="col-num">雲端報告數</th>
<th onclick="sortTable(5)" class="col-num" title="已對應到此建案的 AI 分析報告數量。- 表示尚未對應">AI 辨識數</th>
<th onclick="sortTable(6)" class="col-coverage" title="AI 辨識數 ÷ 雲端報告數">系統處理進度</th>
<th onclick="sortTable(7)" title="即時監測警戒狀態（來自系統 API），日期為最近一次警戒觸發時間">監測警戒 ℹ️</th>
<th onclick="sortTable(8)">最近更新</th>
<th onclick="sortTable(9)" class="col-num">更新間隔</th>
<th onclick="sortTable(10)">同步狀態</th>
</tr>
</thead>
<tbody>{rows_html}
<tr id="emptyStateRow" style="display:none"><td colspan="11" style="text-align:center;padding:40px;color:#9ca3af"><div style="font-size:24px;margin-bottom:8px">🔍</div>找不到符合條件的建案紀錄</td></tr>
</tbody>
</table>
</div>
</div>
</div>

<script>
(function() {{
  // Dynamic date calculation
  const today = new Date();
  today.setHours(0,0,0,0);
  document.querySelectorAll('.days[data-date]').forEach(function(el) {{
    const raw = el.getAttribute('data-date');
    if (!raw || raw === '-') {{ el.textContent = '-'; return; }}
    const parts = raw.split('-');
    if (parts.length !== 3) {{ el.textContent = raw; return; }}
    const d = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
    const diff = Math.floor((today - d) / 86400000);
    el.textContent = diff + ' 天';
    if (diff > 30) {{
      el.classList.add('days-old');
    }} else if (diff <= 7) {{
      el.classList.add('days-recent');
    }}
  }});

  // Mark stale rows dynamically
  document.querySelectorAll('#dataTable tbody tr:not(#emptyStateRow)').forEach(function(row) {{
    const latestDate = row.getAttribute('data-latest-date');
    const status = row.getAttribute('data-status');
    if (latestDate && latestDate !== '-' && status !== 'no_reports') {{
      const parts = latestDate.split('-');
      if (parts.length === 3) {{
        const d = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
        const diff = Math.floor((today - d) / 86400000);
        if (diff > 30) row.classList.add('row-stale');
      }}
    }}
  }});
  // 初始化排序指標
  document.querySelectorAll('#dataTable thead th').forEach(function(th) {{
    if(th.hasAttribute('onclick')) th.classList.add('sortable');
  }});

  // 搜尋防抖（250ms），避免每個按鍵都觸發 500+ 列重算
  let searchTimeout;
  document.getElementById('search').addEventListener('input', function() {{
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(function() {{ filterTable(); }}, 250);
  }});
}})();

let currentFilter = '';
function filterTable() {{
  const search = document.getElementById('search').value.toLowerCase();
  const rows = document.querySelectorAll('#dataTable tbody tr:not(#emptyStateRow)');
  const today = new Date();
  today.setHours(0,0,0,0);
  let visibleCount = 0;
  rows.forEach(function(row) {{
    const permit = row.cells[1].textContent.toLowerCase();
    const name = row.cells[2].textContent.toLowerCase();
    const status = row.dataset.status;
    const cloud = row.dataset.cloud;
    const alertTotal = parseInt(row.dataset.alertTotal || '0');
    const matchSearch = permit.includes(search) || name.includes(search);
    let matchStatus = true;
    if (currentFilter === 'other_cloud') {{
      matchStatus = cloud !== 'Google Drive';
    }} else if (currentFilter === 'needs_attention') {{
      let isStale = false;
      const ld = row.getAttribute('data-latest-date');
      if (ld && ld !== '-' && status !== 'no_reports') {{
        const parts = ld.split('-');
        if (parts.length === 3) {{
          const d = new Date(parseInt(parts[0]), parseInt(parts[1])-1, parseInt(parts[2]));
          isStale = Math.floor((today - d) / 86400000) > 30;
        }}
      }}
      matchStatus = alertTotal > 0 || isStale;
    }} else if (currentFilter) {{
      matchStatus = status === currentFilter;
    }}
    const visible = matchSearch && matchStatus;
    row.style.display = visible ? '' : 'none';
    if (visible) visibleCount++;
  }});
  const esr = document.getElementById('emptyStateRow'); if(esr) esr.style.display = visibleCount === 0 ? 'table-row' : 'none';
}}
function filterStatus(btn, status) {{
  currentFilter = status;
  document.querySelectorAll('.filter-group .btn').forEach(function(b) {{ b.classList.remove('active'); b.setAttribute('aria-pressed','false'); }});
  btn.classList.add('active');
  btn.setAttribute('aria-pressed','true');
  filterTable();
}}
function sortTable(n) {{
  const table = document.getElementById('dataTable');
  const tbody = table.tBodies[0];
  const rows = Array.from(tbody.querySelectorAll('tr:not(#emptyStateRow)'));
  const emptyRow = document.getElementById('emptyStateRow');
  const ths = table.querySelectorAll('thead th');
  const targetTh = ths[n];
  const isAsc = targetTh.classList.contains('sort-asc');
  const dir = isAsc ? -1 : 1;
  ths.forEach(function(th) {{ th.classList.remove('sort-asc', 'sort-desc'); }});
  targetTh.classList.add(dir === 1 ? 'sort-asc' : 'sort-desc');
  rows.sort(function(a, b) {{
    let xText = a.cells[n].textContent.trim();
    let yText = b.cells[n].textContent.trim();
    if (xText === '-' || xText === '') xText = dir === 1 ? '999999999' : '-999999999';
    if (yText === '-' || yText === '') yText = dir === 1 ? '999999999' : '-999999999';
    let xNum = parseFloat(xText.replace(/[^0-9.\\-]/g, ''));
    let yNum = parseFloat(yText.replace(/[^0-9.\\-]/g, ''));
    if (!isNaN(xNum) && !isNaN(yNum) && /[0-9]/.test(xText) && /[0-9]/.test(yText)) {{
      return (xNum - yNum) * dir;
    }}
    return xText.localeCompare(yText, 'zh-TW') * dir;
  }});
  rows.forEach(function(row) {{ tbody.appendChild(row); }});
  if (emptyRow) tbody.appendChild(emptyRow);
}}
function toggleAttention() {{
  const sec = document.getElementById('attentionSection');
  sec.classList.toggle('open');
}}
function toggleLegend() {{
  const sec = document.getElementById('legendSection');
  sec.classList.toggle('open');
}}
</script>
</body>
</html>'''

    if output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"  已生成: {output_path}")
    return html



def generate_csv_report(permit_data: Dict[str, dict], non_google: List[dict], alert_data: Dict[str, dict] = None, permit_names: Dict[str, str] = None, output_path: str = None, gov_url_statuses: Dict[str, str] = None):
    """生成 CSV 報告"""
    print("📄 生成 CSV 報告...")

    if alert_data is None:
        alert_data = {}
    if permit_names is None:
        permit_names = {}
    if gov_url_statuses is None:
        gov_url_statuses = {}

    non_google_set = {item['permit']: item['cloud'] for item in non_google}

    sorted_permits = sorted(permit_data.keys(), key=lambda x: permit_data[x].get('latest_report', '') or '', reverse=True)

    lines = ['序號,建照字號,建案名稱,雲端服務,Drive PDF,系統 PDF,覆蓋率,警戒值項數,行動值項數,最近警戒日期,最新報告,距今天數,狀態,政府PDF URL狀態']

    for i, permit in enumerate(sorted_permits, 1):
        data = permit_data[permit]
        cloud = non_google_set.get(permit, 'Google Drive')
        drive = data.get('drive_count', 0)
        system = data.get('system_count', 0)
        coverage = f"{min(100, int(system/drive*100))}%" if drive > 0 and system > 0 else '-'
        latest = data.get('latest_report', '')[:10] if data.get('latest_report') else ''
        days = data.get('days_since_update', '')
        status = data.get('status', 'unknown')

        # 建案名稱
        building_name = permit_names.get(permit, '')

        # 即時警戒值
        permit_alert = alert_data.get(permit, {})
        warning = permit_alert.get('warning_count', 0)
        danger = permit_alert.get('danger_count', 0)
        latest_alert = permit_alert.get('latest_alert_date', '')[:10] if permit_alert.get('latest_alert_date') else ''

        url_status = gov_url_statuses.get(permit, '')

        lines.append(f'{i},"{permit}","{building_name}","{cloud}",{drive},{system},{coverage},{warning},{danger},{latest_alert},{latest},{days},{status},{url_status}')

    if output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w', encoding='utf-8-sig') as f:
            f.write('\n'.join(lines))
        print(f"  已生成: {output_path}")
    return '\n'.join(lines)

