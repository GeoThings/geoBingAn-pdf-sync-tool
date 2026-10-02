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
from datetime import datetime, timedelta

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


# ---------- 定期實測：把「需登入」從手寫常數換成測出來的 ----------
#
# FAMILY_FINDINGS 是我 2026-09-29 人工探測後**手寫**的結論，有兩個問題：
# 一是會過期（承造人隨時可能改權限，改好了我們不會知道）；二是以家族為單位，
# 同家族裡個別恢復看不出來。
#
# ⚠️ 自動探測只能認 **URL 層級**的證據。2026-09-29 我用 body 關鍵字判斷，把
# Dropbox 與 Synology gofile 都判錯——那些頁面本來就含 signin 字串，是 JS 外殼
# 不是登入牆，最後要用真實瀏覽器才分得出來。所以這裡的分類刻意保守：
#
#   auth_redirect     轉址到已知的登入端點 → 可靠，確定要登入
#   unreachable       連不上／4xx／5xx → 可靠
#   reachable_unknown HTTP 200 但內容可能是 JS 外殼 → **不宣稱任何結論**
#
# 真正的價值在**偵測恢復**：先前 auth_redirect 或 unreachable 的案子變成 200，
# 代表對方可能改好了權限，那是要出聲的事（同 #100 失效資料夾重驗的紀律）。

PROBE_INTERVAL_DAYS = 7          # 每案多久重測一次
PROBE_MAX_HOPS = 5

VERDICT_AUTH = 'auth_redirect'
VERDICT_UNREACHABLE = 'unreachable'
VERDICT_UNKNOWN = 'reachable_unknown'

#: 已知的登入端點特徵。只比對**轉址目的地的 URL**，不比對頁面內容。
_AUTH_URL_MARKERS = (
    '/_forms/default.aspx',
    '/_layouts/15/authenticate.aspx',
    'sharepointerror.aspx',
    'accounts.google.com/signin',
    'accounts.google.com/v3/signin',
    'login.microsoftonline.com',
    'login.live.com',
)


def probe_once(url: str, fetch=None):
    """測一個來源，回 (verdict, note)。只用 URL 層級證據。

    逐跳自己跟轉址：要看的就是「它把我們導去哪」，交給 requests 自動跟就看不到了。
    """
    from geobingan_sync.link_resolver import _http_get
    from urllib.parse import urljoin
    fetch = fetch or _http_get
    current = url
    for _hop in range(PROBE_MAX_HOPS):
        try:
            status, location, _body = fetch(current)
        except Exception as e:                                # noqa: BLE001
            return VERDICT_UNREACHABLE, f'{type(e).__name__}'
        if location:
            nxt = urljoin(current, location)
            low = nxt.lower()
            for marker in _AUTH_URL_MARKERS:
                if marker in low:
                    return VERDICT_AUTH, marker
            current = nxt
            continue
        if status == 200:
            return VERDICT_UNKNOWN, 'http_200'
        return VERDICT_UNREACHABLE, f'http_{status}'
    return VERDICT_UNREACHABLE, f'too_many_hops:>{PROBE_MAX_HOPS}'


def due_for_probe(info: dict, now: datetime = None,
                  interval_days: int = PROBE_INTERVAL_DAYS) -> bool:
    """這一案該不該重測。沒測過或時間戳毀損一律測（fail-open 倒向重測那側）。"""
    raw = (info or {}).get('probed_at')
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return True
    if last.tzinfo is not None:
        last = last.astimezone().replace(tzinfo=None)
    return ((now or datetime.now()) - last) >= timedelta(days=interval_days)


def probe_due(data: dict, now: datetime = None, fetch=None,
              interval_days: int = PROBE_INTERVAL_DAYS, max_probes: int = 0):
    """對到期的案子重測，就地更新 data['sources']，回 (測了幾案, 恢復清單)。

    「恢復」＝先前 auth_redirect／unreachable、這次變成 200。那是對方可能改好權限
    的訊號，必須出聲——不然我們永遠停在三天前的結論上。
    """
    now = now or datetime.now()
    sources = (data or {}).get('sources') or {}
    probed, recovered = 0, []
    for permit in sorted(sources):
        info = sources[permit]
        if not due_for_probe(info, now, interval_days):
            continue
        if max_probes and probed >= max_probes:
            break
        before = info.get('probe_verdict')
        verdict, note = probe_once(info.get('url', ''), fetch=fetch)
        info['probe_verdict'] = verdict
        info['probe_note'] = note
        info['probed_at'] = now.isoformat()
        probed += 1
        if verdict == VERDICT_UNKNOWN and before in (VERDICT_AUTH, VERDICT_UNREACHABLE):
            info['probe_recovered_at'] = now.isoformat()
            recovered.append(permit)
    return probed, recovered


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
        same_url = old.get('url') == url
        first = old.get('first_seen', today) if same_url else today
        host = _host(url)
        entry = {
            'url': url,
            'host': host,
            'family': classify_host(host),
            'note': note or '',
            'first_seen': first,
            'last_seen': today,
        }
        # 探測結果跟著「同一個連結」走。連結換了就作廢重測——舊結論套到新連結
        # 等於憑空宣稱沒測過的事（同 #100 快取鍵必須綁實體 ID 的教訓）。
        if same_url:
            for k in ('probe_verdict', 'probe_note', 'probed_at', 'probe_recovered_at'):
                if old.get(k):
                    entry[k] = old[k]
        sources[permit] = entry
    return {'generated_at': now.isoformat(), 'count': len(sources),
            'first_run': first_run, 'baseline_since': baseline_since, 'sources': sources}


#: 自動探測的 verdict → 人看得懂的說法。只講測得出來的，不替 JS 外殼下結論。
_VERDICT_LABEL = {
    VERDICT_AUTH: '需登入（實測）',
    VERDICT_UNREACHABLE: '連不上（實測）',
    VERDICT_UNKNOWN: '可開啟但未支援（實測）',
}


def summarise(data: dict) -> list:
    """依家族彙總，數量多的在前。回傳 [(family, count, status, evidence_date, note)]。

    有實測結果就用實測（含最後一次測的日期）；同家族內 verdict 不一致時標「混合」。
    沒測過才退回 FAMILY_FINDINGS 的人工結論——那是 2026-09-29 手寫的，會過期，
    所以只當備援。
    """
    sources = (data or {}).get('sources') or {}
    counts, verdicts, probed_dates = {}, {}, {}
    for info in sources.values():
        fam = info.get('family') or '（未分類）'
        counts[fam] = counts.get(fam, 0) + 1
        v = info.get('probe_verdict')
        if v:
            verdicts.setdefault(fam, set()).add(v)
            d = str(info.get('probed_at') or '')[:10]
            if d:
                probed_dates[fam] = max(probed_dates.get(fam, ''), d)
    out = []
    for fam, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        vs = verdicts.get(fam)
        if vs and len(vs) == 1:
            status = _VERDICT_LABEL.get(next(iter(vs)), '未確認')
            out.append((fam, n, status, probed_dates.get(fam, '') or '未探測', ''))
        elif vs:
            label = '、'.join(sorted(_VERDICT_LABEL.get(v, v) for v in vs))
            out.append((fam, n, f'混合：{label}', probed_dates.get(fam, '') or '未探測', ''))
        else:
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
