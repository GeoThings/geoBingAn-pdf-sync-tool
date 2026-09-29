"""記錄「清單上有、但我們接不到」的來源，讓缺口從沉默變成可追蹤的資料。

為什麼需要這支：`_resolve_or_skip_indirect` 解不出 Drive 資料夾、又沒有 adapter
的建案會被**剔除**，理由很好（保留只會建出空資料夾再記一筆同步錯誤），但代價是
那些案從此在系統裡完全消失——沒有名單、沒有原因、沒有時間。要回答「哪些建案拿
不到資料、為什麼」只能每次重跑一輪人工探測。

2026-09-29 逐一探測的結論是，這個缺口**大部分不是工程問題**：承造人交給建管處
的連結需要帳號才看得到，公開揭露形同虛設。那要走對外溝通，而對外溝通需要一份
維護中的名單，不是一次性的探測結果。

⚠️ 名單必須會**縮**。建案日後改了連結、或我們補上 adapter，就要從名單消失；
否則它會變成一份只增不減的舊帳（同 [[失敗快取要有到期日]] 的教訓：不可把一次
觀測當成永久事實）。所以每輪都以「本輪實際被跳過的集合」為準重寫。
"""
import json
import os
import re
from datetime import datetime

UNSUPPORTED_FILE = './state/unsupported_sources.json'

_IPV4 = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')


def classify_host(host: str) -> str:
    """把主機歸到一個家族。純粹依主機名分類，**不是**對可用性的判斷。"""
    h = (host or '').lower().strip()
    if not h:
        return '（無主機）'
    if h.endswith('.sharepoint.com') or h == 'sharepoint.com':
        return 'SharePoint'
    if h in ('1drv.ms', 'onedrive.live.com'):
        return 'OneDrive'
    if h == 'gofile.me' or h.endswith('.quickconnect.to'):
        return 'Synology 分享'
    if h == 'dropbox.com' or h.endswith('.dropbox.com'):
        return 'Dropbox'
    if h == 'mega.nz' or h.endswith('.mega.nz'):
        return 'MEGA'
    if h.endswith('.pcloud.link') or h.endswith('.pcloud.com'):
        return 'pCloud'
    if h.endswith('.google.com') or h == 'google.com':
        return 'Google（非資料夾）'
    if _IPV4.match(h):
        return '自架主機（裸 IP）'
    return f'其他：{h}'


#: 人工探測結論。**點時間的觀察，不是永久事實**——所以每一筆都帶日期與樣本數，
#: 輸出時一律連日期一起顯示，讓讀的人看得到它多舊。只寫實際看到的，沒看到的寫
#: 「未確認」，不要從同家族推論（[[機制可證 ≠ 成因已證]]）。
FAMILY_FINDINGS = {
    'SharePoint': ('需登入', '2026-09-29', '全數轉址到 /_forms/default.aspx 登入頁（16/16 自動探測）'),
    'Synology 分享': ('需登入', '2026-09-29', '瀏覽器實測顯示「安全存取／使用 DSM 帳號」（抽驗 1 案，其餘回應相同）'),
    'Dropbox': ('公開可列檔', '2026-09-29', '瀏覽器實測可列出檔名與日期（抽驗 1 案）；尚未做 adapter'),
    'pCloud': ('公開可列檔', '2026-09-29', '官方 showpublink API 可列出 44 份 PDF；尚未做 adapter'),
    '自架主機（裸 IP）': ('多數連不上', '2026-09-29', '4 案中 3 案連線逾時或 404'),
    'MEGA': ('未確認', '2026-09-29', '頁面可開，未驗證能否列檔；內容端對端加密'),
    'OneDrive': ('未確認', '2026-09-29', '回 200 但內容由 JS 載入，未用瀏覽器確認'),
    'Google（非資料夾）': ('部分需登入', '2026-09-29', '4 案中 2 案轉址到 accounts.google.com；另 2 案是單檔/文件連結'),
}


def _host(url: str) -> str:
    m = re.match(r'https?://([^/:?#]+)', url or '', re.I)
    return m.group(1).lower() if m else ''


def build(entries, previous=None, now=None) -> dict:
    """用**本輪實際被跳過的集合**重建名單，保留既有的 first_seen。

    entries: [(permit, url, note)]
    """
    now = now or datetime.now()
    today = now.strftime('%Y-%m-%d')
    prev = ((previous or {}).get('sources') or {})
    # 沒有上一輪就沒有基準，這輪的 first_seen 全是今天——那不代表它們「剛失聯」。
    first_run = not bool(previous)
    # 只標 first_run 還不夠：基準剛建立的那幾天，所有 first_seen 都還很新，
    # 「近 14 天新增」會把整份名單都算成新事故（實測第二輪就誤報 41 件）。
    # 所以記下基準日，判定新增時比的是「**基準建立之後**才出現」。
    baseline_since = (previous or {}).get('baseline_since') or today
    sources = {}
    for permit, url, note in entries:
        old = prev.get(permit) or {}
        # 連結換了就是新情況，first_seen 要重算，否則會謊報「已經壞很久」
        first = old.get('first_seen', today) if old.get('url') == url else today
        host = _host(url)
        sources[permit] = {
            'url': url,
            'host': host,
            'family': classify_host(host),
            'note': note or '',
            'first_seen': first,
            'last_seen': today,
        }
    return {'generated_at': now.isoformat(), 'count': len(sources),
            'first_run': first_run, 'baseline_since': baseline_since, 'sources': sources}


def summarise(data: dict) -> list:
    """依家族彙總，數量多的在前。回傳 [(family, count, status, evidence_date, note)]。"""
    sources = (data or {}).get('sources') or {}
    counts = {}
    for info in sources.values():
        fam = info.get('family') or '（未分類）'
        counts[fam] = counts.get(fam, 0) + 1
    out = []
    for fam, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        status, date, note = FAMILY_FINDINGS.get(fam, ('未確認', '未探測', ''))
        out.append((fam, n, status, date, note))
    return out


def load(path: str = None) -> dict:
    try:
        with open(path or UNSUPPORTED_FILE, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(data: dict, path: str = None) -> None:
    """原子寫入。半份 JSON 會讓下一輪讀不到 first_seen，名單的年齡就全歸零。"""
    p = path or UNSUPPORTED_FILE
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f'{p}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
