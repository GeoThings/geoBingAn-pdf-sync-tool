#!/usr/bin/env python3
"""
建築執照監測資料同步工具 v5.0 (智慧分塊版)
核心升級：
1. [智慧分塊演算法] 改用「領地搜尋法」解析 PDF：
   - 先定位所有建照號碼的位置。
   - 在兩個號碼之間的區域搜尋「任何」Google Drive 連結。
   - 自動修復像 112-0238 這種因網址格式特殊而漏抓的建案。
2. [容錯率提升] 不再依賴嚴格的網址 Regex，大幅降低漏抓機率。
3. [功能保留] 包含隨機跳查、斷點續傳、自動建立資料夾等所有功能。
"""
import json
from geobingan_sync import REPO_ROOT
from geobingan_sync import sync_errors
from geobingan_sync.city_config import get_cities_for_cli
import os
import csv
import re
import requests
import urllib3
import time
import io
import sys
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
import pypdf
from typing import Dict, List, Tuple

import warnings

# ================== 設定區域 ==================
# 請確認金鑰路徑是否正確
SERVICE_ACCOUNT_FILE = os.environ.get(
    'GOOGLE_CREDENTIALS',
    str(REPO_ROOT / 'credentials.json')
)
SCOPES = ['https://www.googleapis.com/auth/drive']

# 從 config.py 匯入 Shared Drive ID
try:
    from geobingan_sync.config import SHARED_DRIVE_ID
except ImportError:
    SHARED_DRIVE_ID = os.environ.get('SHARED_DRIVE_ID', '0AIvp1h-6BZ1oUk9PVA')
PDF_LIST_URL_DEFAULT = 'https://www-ws.gov.taipei/001/Upload/845/relfile/-1/845/03b35db7-a123-4b29-b881-1cb17fa9c4f2.pdf'
STATE_FILE = './state/sync_permits_progress.json'
PERMIT_LIST_PATH = '/tmp/permit_list.pdf'   # 下載政府清單的預設落地路徑（測試請用 dest= 注入）
# ============================================

# Google Drive API 認證（lazy init，避免 import 時就需要 credentials.json）
# credentials 是 thread-safe 的，但 httplib2.Http 不是。
# 每個 thread 需要自己的 service instance。
# https://googleapis.github.io/google-api-python-client/docs/thread_safety.html
_credentials = None
_drive_service = None
_thread_local = threading.local()


def _get_credentials():
    global _credentials
    if _credentials is None:
        _credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return _credentials


def get_drive_service():
    """取得主執行緒的 Drive service（lazy init）"""
    global _drive_service
    if _drive_service is None:
        _drive_service = build('drive', 'v3', credentials=_get_credentials())
    return _drive_service


def get_thread_drive_service():
    """取得當前 thread 的獨立 Drive service instance"""
    if not hasattr(_thread_local, 'service'):
        _thread_local.service = build('drive', 'v3', credentials=_get_credentials())
    return _thread_local.service

# 並行處理設定
MAX_CONCURRENT_PERMITS = 5  # 同時處理的建案數（Google Drive API quota: 12,000 req/min）

#: `source_scan` 的 reason 分兩類，這一類是「這輪沒量到」，只能寫 `pdf=None`。
#: 定在模組層並由 `_record_source_scan` 自己擋，不靠 10 個呼叫點各自記得——
#: 未取得與實測 0 混同就是把告警變成靜默，那條不變式不可只活在測試裡
#: （同 feedback_enforce_invariant_at_boundary）。
UNKNOWN_SCAN_REASONS = frozenset({'list_failed', 'invalid_url', 'partial',
                                  'not_measured', 'source_gone'})

# 鏡像與「算不算可上傳的 PDF」的判定只有一份，在 geobingan_sync.mirror_types。
# 三個呼叫端（這裡、upload_pdfs、match_permits）共用——各寫一套的後果見該模組
# 的 docstring（#116 review P1：一份真的報告會永遠不上傳，而且帳上看不出來）。
from geobingan_sync.mirror_types import (PDF_MIME, canonical_mime,      # noqa: E402
                                         is_uploadable_pdf, should_mirror)


from geobingan_sync.config import escape_drive_query as _escape_drive_query
from geobingan_sync.permit_utils import normalize_permit as _normalize_permit


def resolve_list_pdf_url(page_url: str, timeout: int = 30):
    """從建管處「建築工地監測數據雲端資料庫清單」發布頁動態解析當前清單 PDF 連結。

    頁面以 `Download.ashx?u=<base64 檔案路徑>&n=<base64 檔名>` 連結提供清單；政府
    改版時會換成新 relfile 路徑（檔名常帶民國日期，如 表單回復_1150902.pdf），所以
    寫死 pdf_list_url 會一直抓到舊版。此處抓頁面、取出該 PDF 下載連結。

    回傳 (absolute_url, filename) 或 None（任何失敗都回 None，由呼叫端 fallback
    到設定的靜態 pdf_list_url，確保 nightly 不會因頁面改版而中斷）。
    """
    import base64
    import urllib.parse
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', category=urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(page_url, headers={'User-Agent': 'Mozilla/5.0'},
                                timeout=timeout, verify=False)
        resp.raise_for_status()
        html = resp.text
        # 取頁面上所有 Download.ashx 連結，挑檔名/路徑為 PDF 的第一個
        for raw in re.findall(r'href="([^"]*Download\.ashx\?[^"]*)"', html):
            href = raw.replace('&amp;', '&')
            fname = ''
            m = re.search(r'[?&]n=([^&]+)', href)
            if m:
                try:
                    fname = base64.b64decode(urllib.parse.unquote(m.group(1)) + '==').decode('utf-8', 'ignore')
                except Exception:
                    fname = ''
            if fname.lower().endswith('.pdf') or '.pdf' in href.lower():
                return urllib.parse.urljoin(page_url, href), fname
        return None
    except Exception:
        return None


# 頁尾殘留特徵：網址結尾是「數字/數字」。只用來出聲提醒，不用來修剪——
# 真實網址結尾也可能長得像 `/12/30`，靠樣式猜會誤傷。
_PAGE_FOOTER_LEAK = re.compile(r'\d{1,3}/\d{1,3}$')


def strip_page_footer(text: str, page_no: int, total_pages: int) -> str:
    r"""移除單頁文字結尾的頁碼（形如 ``19 / 37``）。

    為什麼非移不可：parse_pdf_list 會把**所有空白**清掉，才能把被 PDF 斷行的
    網址接回來。頁碼因此直接黏在該頁最後一個網址尾巴上——
    ``...VjC1l`` + ``19 / 37`` → ``...VjC1l19/37``。

    黏在 query string 後面只是雜訊，但**黏在 Drive folder ID 尾巴上會把 ID 改壞**，
    打 Drive API 必然 404，我們就把活的來源判成死的。2026-09-29 實測台北市清單
    440 案中 19 個 folder ID 被改壞，其中 14 個資料夾其實讀得到、已累積 791 份
    PDF，卻全被標成失效、從此收不到新報告。

    比對的是**這一頁真正的頁碼**（``page_no``／``total_pages``），不是猜
    ``\d+/\d+`` 樣式——真實網址結尾也可能長得像 ``/12/30``，用樣式修剪會誤傷。

    光比對頁碼還不夠（review P2）：第 19 頁的合法網址若正好以 ``/19/37`` 結尾，
    數字就跟頁碼完全一樣，只看數字分不出來。因此**要求頁碼前有空白邊界或位於
    字串開頭**——實際 PDF 每頁都是 ``...網址\n19 / 37``，37 頁無一例外。沒有
    邊界時代表抽取結果把兩者黏成一團，已經無法與合法網址區分，寧可保守不剪；
    parse_pdf_list 解析完會掃殘留特徵並出聲，不會無聲吞掉。
    """
    page, total = re.escape(str(page_no)), re.escape(str(total_pages))
    return re.sub(rf'(?:^|\s)\s*{page}\s*/\s*{total}\s*$', '', text)


class PermitSync:
    def __init__(self, city: dict = None):
        self.city = city or {}
        self.city_name = self.city.get('name', '台北市')
        self.source_type = self.city.get('source_type', 'pdf')
        self.pdf_list_url = self.city.get('pdf_list_url') or PDF_LIST_URL_DEFAULT
        # 發布頁 URL：政府會不定期把清單換成新檔（新 relfile 路徑），寫死
        # pdf_list_url 會一直抓到舊版清單。有設 list_page_url 時，download_pdf_list
        # 會先從發布頁動態解析當前清單連結，解析失敗才 fallback 回 pdf_list_url。
        self.list_page_url = self.city.get('list_page_url', '')
        # 本次實際使用的清單身分（供 list_fingerprint 記錄；'動態' 表示從發布頁解析成功）
        self.list_source = ''
        self.list_label = ''
        # 清單 PDF 裡**實際存在**的建照號（不管有沒有解析出連結）。指紋要用這個：
        # 用「有連結的」當指紋，等於把「對方的清單」和「我方的解析能力」混成同一
        # 個數字，我們改解析器就會被記成對方改清單（2026-09-30 實際誤報一次）。
        self.permits_in_list = []
        self.csv_path = self.city.get('csv_path', '')
        self.shared_drive_id = self.city.get('shared_drive_id') or SHARED_DRIVE_ID
        self.target_folders = {}
        self.permit_mapping = {}
        # 本輪實際新增的數量。以前靠 shell grep 日誌反推，樣式漂掉就靜默歸零
        # （2026-09-30 查出累計 119 次執行都報 0）。改由這裡累加後寫成資料。
        self.copied_total = 0
        self.adapter_uploaded = 0
        self.adapter_failed = 0
        self.permits_with_new = 0
        self.state = self.load_state()
        self.restricted_files = []
        # 本輪**實際走訪**與**出過錯**的案。連續失敗計數在輪次結束時依這兩個集合
        # 結算，不在事件層加減——adapter 路徑會對同一案同時記錯誤又標 processed，
        # 事件層加減一旦順序顛倒就永遠是 0，而那是靜默的。
        self._visited_permits = set()
        self._errored_permits = set()
        #: run() 層級的失敗訊息（掃描逾時這類不屬於任何單一建案的錯誤）
        self._run_error = None
        self._state_lock = threading.Lock()
        self._print_lock = threading.Lock()
        self._target_file_cache = {}
        self._subfolder_cache = {}
        
    def load_state(self) -> dict:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
            # 向後相容：舊版 processed 是 list，新版是 dict
            if isinstance(state.get('processed'), list):
                state['processed'] = {p: '' for p in state['processed']}
            return state
        return {'processed': {}, 'skipped': [], 'errors': [], 'restricted': []}
    
    def save_state(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with self._state_lock:
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.state, indent=2, ensure_ascii=False, fp=f)

    def _mark_visited(self, permit_no: str):
        """這一案這輪真的走訪過了。只有走訪過的才參與連續失敗結算。"""
        with self._state_lock:
            self._visited_permits.add(permit_no)

    def _record_error(self, permit_no: str, error):
        """唯一的錯誤落檔入口——時間戳規則只有一份。

        四個呼叫點（bundle 寫入失敗／adapter 取檔失敗／sync_permit 例外／
        executor 未預期錯誤）都走這裡。留在呼叫點自律一定會漏：executor 那個
        分支原本只 print 不落檔，等於那類錯誤從來沒進過資料。
        """
        with self._state_lock:
            sync_errors.record_error(self.state, permit_no, error,
                                     run=os.environ.get('SYNC_RUN_STARTED') or None)
            self._errored_permits.add(permit_no)

    def _record_source_scan(self, permit_no: str, *, pdf, other, reason: str,
                            skipped=None, now: datetime = None):
        """唯一的「來源量測結果」落檔入口。

        2026-10-12 查 12 案「清單內、接得到、卻零紀錄」時發現：程式其實**量到了**
        結果（列完資料夾、裡面沒有 PDF），但那是最強等級的證據，卻只印進日誌就
        return，沒寫成資料。於是 57 個「實測 0 份」在帳上和「從沒量過」長得一模
        一樣，要靠人工逐案探測才分得出來——正是「計數要由產生端寫成資料、不從
        日誌反推」那條教訓。

        `pdf=None` 專指「這輪沒量到」（列出失敗／URL 解析不出來），絕不可寫成 0；
        未取得與實測 0 混同，就是把告警變成靜默。這裡直接把 reason 與 pdf 的
        一致性擋掉：reason 屬於 `UNKNOWN_SCAN_REASONS` 就只能是 None，其餘
        reason 就必須是數字。呼叫點有 10 個，其中三個的 reason 是條件運算式
        （掃原始碼的守門測試看不進常數以外的形狀），靠自律遲早漏。

        `other=None` 是第三種狀態：**非 PDF 檔數沒量到**。adapter 路徑在列檔時
        就把非 PDF 丟掉了（見各 adapter 的 `.pdf` 過濾），那個數字我們拿不到，
        寫 0 等於宣稱「看過、沒有」——和這支函式要防的錯誤是同一個。

        `other` 與 `skipped` 的分工：`other` 是**鏡像進雲端硬碟**的非 PDF 檔數
        （圖片、Excel），`skipped` 是**連鏡像都不收**的型別。只有 `pdf` 那一欄
        會上傳到 riskmap，三個數字分開記才答得出「我們手上有什麼」與
        「後端看得到什麼」這兩個不同的問題。
        """
        if (reason in UNKNOWN_SCAN_REASONS) != (pdf is None):
            raise ValueError(
                f'{permit_no}: reason={reason} 與 pdf={pdf!r} 不一致——'
                f'{sorted(UNKNOWN_SCAN_REASONS)} 代表這輪沒量到、只能寫 None；'
                '其餘 reason 代表量到了、必須寫數字')
        entry = {'at': (now or datetime.now()).isoformat(),
                 'reason': reason,
                 'other': None if other is None else int(other),
                 'skipped': None if skipped is None else int(skipped)}
        entry['pdf'] = None if pdf is None else int(pdf)
        with self._state_lock:
            self.state.setdefault('source_scan', {})[permit_no] = entry

    @staticmethod
    def _scan_result(scan: dict, pdf_files: list, other_files: list) -> dict:
        """把一次掃描的結果換成 `_record_source_scan()` 的參數。

        reason 與三個數字由**同一個地方**算出來。原本是兩處各推一遍（空清單那條
        與成功路徑那條），加進「非 PDF 也要鏡像」之後兩條路會交會，分頭推導就會
        得到互相矛盾的結論。

        `pdf` 只算 PDF：它代表的是「會上傳到 riskmap 的份數」。把 13 個 .xls
        算進去，就等於宣稱這案有 13 份報告在收。
        """
        failed = scan.get('failed', 0)
        if scan.get('missing'):
            # 資料夾已經不存在了：這不是實測 0，是量不到。擺在 `failed` 之前，
            # 因為「來源不見了」比「有幾個子資料夾列不出來」更確定。
            reason, pdf = 'source_gone', None
        elif failed:
            reason = 'partial' if (pdf_files or other_files) else 'list_failed'
            pdf = None
        elif pdf_files:
            reason, pdf = 'ok', len(pdf_files)
        elif other_files or scan.get('skipped', 0):
            reason, pdf = 'no_pdf_but_other', 0
        else:
            reason, pdf = 'empty', 0
        return {'pdf': pdf, 'other': len(other_files), 'reason': reason,
                'skipped': scan.get('skipped', 0)}

    def _print(self, msg: str):
        """Thread-safe print"""
        with self._print_lock:
            print(msg, flush=True)
    
    def load_csv_list(self) -> Dict[str, str]:
        """從 CSV 檔案載入建照號碼 → Drive URL 對應"""
        print(f"📄 載入 CSV: {self.csv_path}")
        mapping = {}
        csv_file = Path(self.csv_path)
        if not csv_file.is_absolute():
            csv_file = REPO_ROOT / self.csv_path
        if not csv_file.exists():
            print(f"❌ CSV 檔案不存在: {csv_file}")
            return mapping
        with open(csv_file, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                permit_no = row.get('permit_no', '').strip()
                source_url = row.get('source_url', '').strip()
                if permit_no and source_url:
                    mapping[permit_no] = source_url
        print(f"✅ 載入 {len(mapping)} 個建案")
        return mapping

    def download_pdf_list(self, max_attempts: int = 3, dest: str = None) -> str:
        # retry-with-backoff 防 transient 網路/DNS 失敗（#59）— 配合 network_ready.py
        # 的 post-wake gate，雙層防禦：probe 等 DNS ready，此處再吸收 mid-run blip
        print("📥 下載建案列表 PDF...")
        # 候選 URL 依序嘗試：先動態解析的最新清單，失效再退回靜態 pdf_list_url。
        # 動態 URL 可能解析成功卻已失效（政府又換檔→404/500 或回傳 HTML 錯誤頁），
        # 所以每個候選各自 retry，耗盡後換下一個，而非卡死在動態 URL。
        candidates = []
        if self.list_page_url:
            resolved = resolve_list_pdf_url(self.list_page_url)
            if resolved:
                dyn_url, fname = resolved
                print(f"🔗 動態解析到最新清單：{fname or dyn_url}")
                candidates.append(('動態', dyn_url, fname or dyn_url.rsplit('/', 1)[-1]))
            else:
                print("⚠️  發布頁動態解析失敗，改用靜態 pdf_list_url（可能非最新版）")
        if self.pdf_list_url and self.pdf_list_url not in [u for _, u, _ in candidates]:
            candidates.append(('靜態', self.pdf_list_url, self.pdf_list_url.rsplit('/', 1)[-1]))

        # dest 可注入：測試若寫進正式路徑，會在跑過測試的機器留下假清單檔，
        # 並污染後續以該檔做的人工判讀（2026-09-16 曾因此誤判建案已下架）。
        pdf_path = dest or PERMIT_LIST_PATH
        last_err = None
        for label, url, display in candidates:
            for attempt in range(1, max_attempts + 1):
                try:
                    with warnings.catch_warnings():
                        warnings.filterwarnings('ignore', category=urllib3.exceptions.InsecureRequestWarning)
                        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'},
                                                verify=False, timeout=30)
                    # 驗 HTTP 狀態 + 內容真的是 PDF（擋 HTTP 200 的 HTML 錯誤頁）
                    response.raise_for_status()
                    if not response.content[:5].startswith(b'%PDF'):
                        raise ValueError(f'回應非 PDF（前 16 bytes: {response.content[:16]!r}）')
                    with open(pdf_path, 'wb') as f:
                        f.write(response.content)
                    print(f"✅ 列表已下載（{label}）: {len(response.content)} bytes")
                    self.list_source, self.list_label = label, display
                    return pdf_path
                except Exception as e:
                    last_err = e
                    if attempt < max_attempts:
                        wait = 5 * attempt
                        print(f"⚠️  下載失敗（{label} 第 {attempt}/{max_attempts} 次）: {e}；{wait}s 後重試")
                        time.sleep(wait)
            print(f"⚠️  {label} URL 重試耗盡，改試下一個候選…")
        # 原本是 sys.exit(1)。SystemExit 繼承 BaseException，run() 的
        # `except Exception` 接不到 → _run_error 留 None，健康檢查只能說
        # 「原因未記錄」；城市迴圈的 except Exception 也接不到，後續城市直接中止。
        # 改拋具體例外，讓兩層都接得到（review #112 P2）。
        raise RuntimeError(f'下載建案列表失敗（所有候選皆失敗）: {last_err}')
    
    def parse_pdf_list(self, pdf_path: str) -> Dict[str, str]:
        print("\n📖 解析 PDF 列表 (智慧分塊演算法)...")
        with open(pdf_path, 'rb') as f:
            pdf_reader = pypdf.PdfReader(f)
            pages = pdf_reader.pages
            total = len(pages)
            all_text = ''.join(
                strip_page_footer(p.extract_text() or '', i, total)
                for i, p in enumerate(pages, 1))

        # 移除空白，接合斷行（網址常被 PDF 斷行，所以連空白一起清掉）
        clean_text = re.sub(r'\s+', '', all_text)
        permit_mapping = {}

        # 步驟 1: 找出所有「建照號碼」的位置 (插旗)
        # 使用 iterator 記錄每一個 match 的 start/end 位置
        permit_matches = list(re.finditer(r'(\d{2,3}建字第\d{3,5}號)', clean_text))
        
        if not permit_matches:
            print("❌ 錯誤: 未找到任何建照號碼，請檢查 PDF 內容")
            return {}

        count_found = 0
        count_missed = 0
        # 與 permit_mapping 用同一套正規化，兩邊的集合才可比較
        self.permits_in_list = sorted({_normalize_permit(m.group(1)) or m.group(1)
                                       for m in permit_matches})

        # 步驟 2: 遍歷每個建案，搜尋它「領地」內的網址
        for i in range(len(permit_matches)):
            current_match = permit_matches[i]
            permit_no = current_match.group(1)
            
            # 定義搜尋範圍 (Chunking)
            # 起點：當前建号的结束位置
            start_pos = current_match.end()
            
            # 終點：下一個建号的開始位置 (如果是最後一個，則搜到字串結尾)
            if i < len(permit_matches) - 1:
                end_pos = permit_matches[i+1].start()
            else:
                end_pos = len(clean_text)
            
            # 提取這段區域的文字
            chunk_text = clean_text[start_pos:end_pos]
            
            # 步驟 3: 在區域內搜尋**任何** http(s) 連結
            #
            # 舊版只抓 `https://drive.google.com` 開頭，於是 Google Sites／SharePoint／
            # gofile／Dropbox／Synology 等 20 幾種空間在這一步就被當成「無連結」丟掉
            # （實測 439 案中 71 案）。PR #95 的 link_resolver 因此收不到料——只拿到 2 個
            # 候選而不是 73 個，等於白做。抓進來、交給下游判斷能不能用才對。
            #
            # 字元集用 RFC 3986 的允許字元（含 `:`）。上面已把所有空白移除，所以邊界
            # 靠中文字自然截斷；少了 `:` 的話 SharePoint 的 `/:f:/g/...` 會在第一個冒號
            # 就被切斷。
            url_match = re.search(r"(https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+)", chunk_text)

            if url_match:
                url = url_match.group(1)
                normalized = _normalize_permit(permit_no) or permit_no
                permit_mapping[normalized] = url
                count_found += 1
            else:
                count_missed += 1

        # 回歸偵測：頁碼格式若哪天變了（例如改成「第 1 頁，共 37 頁」），
        # strip_page_footer 會靜靜地失效，網址又開始被汙染而沒人知道。
        # 這裡掃一次殘留特徵，讓它至少出聲。
        leaked = [p for p, u in permit_mapping.items() if _PAGE_FOOTER_LEAK.search(u)]
        if leaked:
            print(f"  ⚠️ {len(leaked)} 個網址結尾疑似殘留頁碼（頁尾格式可能已變）："
                  f"{'、'.join(leaked[:5])}")

        print(f"✅ 解析完成: 成功配對 {len(permit_mapping)} 個 (無連結/無效: {count_missed} 個)")
        
        return permit_mapping
    
    def _record_list_fingerprint(self):
        """記錄本次清單指紋；內容變動時發一則資訊性通知。

        指紋讓 health_check 能看出兩件事：動態解析是否失效（退回靜態＝可能同步
        過期清單）、清單是否長期沒更新。變更本身是好消息、不是問題，所以走
        資訊性通知（不 @、不進 alert_state 去重），且只在內容真的改變時發一次。
        """
        from geobingan_sync.list_fingerprint import ListFingerprint
        fp = ListFingerprint()
        # 指紋是 fail-closed 的核心狀態，**寫入失敗不可吞掉繼續**：若本輪其實已退回
        # 靜態舊清單、指紋卻沒寫成功，health_check 會讀到上一輪的 source=動態、
        # 看不出這次的 fallback——正是 B1 要消除的 silent fallback。例外往上傳，
        # 讓同步步驟失敗（shell 會記 error 並告警），不要拿可能過期的清單繼續掃描。
        # 指紋取「PDF 裡的建照號」而非「解析出連結的建照」。涵蓋率屬於我方能力，
        # 記在 unsupported_sources 名單與同步日誌，不該混進對方清單的指紋。
        changed, summary, state = fp.update(
            self.list_label, self.list_source,
            # 刻意不寫 `or permit_mapping.keys()` 當回退：那會在 permits_in_list
            # 為空時靜默用回錯的基準，正是這支要修的問題。缺欄位就大聲壞掉。
            self.permits_in_list,
            permits_with_links=len(self.permit_mapping))
        print(f"🧾 清單指紋: {state['label']}（{state['source']}，{state['permit_count']} 筆建照）")
        if state.get('basis_migrated_at') == state.get('last_checked'):
            print(f"🔀 指紋基準已從 {state.get('basis_migrated_from')} 遷移為 {state.get('basis')}"
                  f"（不視為清單變更，未推進 last_changed）")
        dropped = state.get('basis_migration_discarded_notices') or []
        if dropped and state.get('basis_migrated_at') == state.get('last_checked'):
            # 丟棄不可靜默：舊基準的待送通知無法在新基準下驗證，但要讓人知道丟了什麼
            print(f"   ⚠️ 同時丟棄舊基準留下的 {len(dropped)} 則待送通知（無法在新基準下驗證）:")
            for d in dropped[:3]:
                print(f"      · {d[:100]}")
        if changed and summary:
            print(f"🆕 {summary}")

        # 相對地，變更通知是 best-effort：送達才清 pending，失敗保留、下輪重試
        pending = state.get('pending_notices') or []
        if pending:
            try:
                if self._send_list_change_notice(pending):
                    fp.clear_pending()
                else:
                    print(f"  （清單更新通知未送達，保留 {len(pending)} 則待下輪重試）")
            except Exception as e:
                print(f"  （清單更新通知處理失敗，保留待下輪重試）: {e}")

    @staticmethod
    def _send_list_change_notice(notices) -> bool:
        """發送清單變更通知；只有 ClickUp 通道成功才算送達（其他通道不算數）。"""
        try:
            from geobingan_sync.notify import send_notification
            results = send_notification('🆕 建管處清單已更新', '\n'.join(notices),
                                        use_clickup=True, mention=False)
            return any(ch == 'ClickUp' and ok for ch, ok in (results or []))
        except Exception as e:
            print(f"  （清單更新通知發送例外）: {e}")
            return False

    def scan_shared_drive(self) -> Dict[str, str]:
        print(f"\n📂 掃描共享雲端...")
        from geobingan_sync.drive_utils import list_top_level_folders
        raw_folders = list_top_level_folders(get_drive_service(), self.shared_drive_id)
        return {item['name']: item['id'] for item in raw_folders}
    
    # 只有 Google Drive 的網址才談得上 folder id。清單裡還有 SharePoint／Dropbox／
    # Synology／mega 等 20 幾種空間，它們的網址常帶 `?id=` 或 `?oid=`，
    # 舊版的寬鬆 `id=` 後備規則會從中抓出假的 folder id（實測 `?oid=AbC123XyZ`
    # 也會中），然後拿它去查 Drive——查不到還算好，查到別人的資料夾更糟。
    _DRIVE_HOSTS = ('drive.google.com', 'docs.google.com')

    def extract_folder_id_from_url(self, url: str) -> str:
        if not url:
            return None
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or '').lower()
        if not any(host == h or host.endswith('.' + h) for h in self._DRIVE_HOSTS):
            return None

        # 支援 /folders/ID 和 /open?id=ID 兩種格式
        match = re.search(r'/folders/([a-zA-Z0-9_-]+)', url)
        if match: return match.group(1)

        match_id = re.search(r'(?:[?&])id=([a-zA-Z0-9_-]+)', url)
        if match_id: return match_id.group(1)

        return None
    
    def _source_folder_state(self, folder_id: str) -> str:
        """回答「這個來源資料夾還在嗎」：`alive` / `gone` / `unknown`。

        為什麼需要單獨問一次：列檔用的查詢是 `'<id>' in parents`，資料夾被刪掉
        之後這個查詢**不會報錯**，只是回零筆。於是「資料夾已經不存在」和「資料夾
        是空的」在列檔結果上長得一模一樣——2026-10-14 有 14 案因此被記成實測
        0 份，而它們的來源 URL 正是 registry 列管的失效連結；那個錯誤最後變成
        對外講「承造人的資料夾開著但裡面沒東西」。

        只在「真的零筆」時才呼叫（有檔案或有跳過的型別就證明資料夾還在），所以
        每輪多出來的 API 呼叫只有十幾次。

        分不出來一律回 `unknown`，由呼叫端當成未取得：403 是權限被收回、5xx
        是對方的問題，兩者都不足以斷定「不存在」。只有 404 與 trashed 才算
        `gone`——斷言失效會讓這案進失效名單，寧可下一輪再確認。
        """
        try:
            meta = self._get_svc().files().get(
                fileId=folder_id, fields='id, trashed',
                supportsAllDrives=True).execute()
        except HttpError as e:
            status = getattr(getattr(e, 'resp', None), 'status', None)
            return 'gone' if status == 404 else 'unknown'
        except Exception:                                       # noqa: BLE001
            return 'unknown'
        return 'gone' if meta.get('trashed') else 'alive'

    def list_files_recursive(self, folder_id: str, path: str = "",
                             scan: dict = None) -> List[Tuple[str, str, str, str, str]]:
        """遞迴列出資料夾裡**要鏡像**的檔案（PDF、圖片、Excel）。

        回傳 `(id, name, path, web_link, mime_type)`。mimeType 要帶回去：
        `copy_file` 的下載回退路徑得用真實型別重建檔案，寫死 PDF 會把一張 jpg
        標成 application/pdf 落進目標資料夾。

        `scan` 是可選的量測累加器。空的回傳值有兩種完全不同的成因——來源真的
        沒有東西、或是 API 列出失敗——呼叫端不能把兩者混成同一個結論（前者是
        實測 0，後者是未取得）。這裡用 `scan` 把過程中的事實帶回去：

        - `scan['skipped']`：**不鏡像**的檔案數（.doc、.zip 這類）。
          注意 `other`（非 PDF 但有鏡像）由呼叫端從回傳值自己算，不在這裡累加
          ——同一個數字只有一個來源，不會兩邊各算一份而漂開。
        - `scan['failed']`：列出失敗的資料夾數。非 0 就表示這次的清單不完整，
          呼叫端必須 fail-open 當成「未取得」，不可寫成實測 0。
        """
        files = []
        try:
            # 完整翻頁（#67）：來源資料夾 >1000 檔時，未翻頁的單次查詢會
            # 靜默截斷第 1001 項起的檔案與子資料夾，PDF 永遠不會被複製。
            from geobingan_sync.drive_utils import paginate_files_list
            query = f"'{folder_id}' in parents and trashed=false"
            items = paginate_files_list(
                self._get_svc(),
                q=query, fields='files(id, name, mimeType, webViewLink)',
                supportsAllDrives=True, includeItemsFromAllDrives=True
            )

            for item in items:
                item_path = f"{path}/{item['name']}" if path else item['name']
                if item['mimeType'] == 'application/vnd.google-apps.folder':
                    subfolder_files = self.list_files_recursive(item['id'], item_path, scan)
                    files.extend(subfolder_files)
                elif should_mirror(item['name'], item['mimeType']):
                    # 第 5 欄放**來源原值**，不是正規化後的型別。
                    # 寫成正規化值會把「副檔名與 mimeType 互相矛盾」這個訊號
                    # 抹掉：`監測資料.pdf` + `image/jpeg` 正規化成
                    # application/pdf 之後，分類端就看不出它其實有矛盾，於是
                    # 會把它算成可上傳的報告——我自己在這個 PR 裡先踩了一次。
                    # 要正規化的地方（copy_file 的重建型別）自己呼叫
                    # canonical_mime，判定需要的原始事實保留在資料裡。
                    files.append((item['id'], item['name'], path,
                                  item.get('webViewLink', ''), item['mimeType']))
                elif scan is not None:
                    scan['skipped'] = scan.get('skipped', 0) + 1
        except HttpError as e:
            self._print(f"  ⚠️ 列出檔案失敗: {e}")
            if scan is not None:
                scan['failed'] = scan.get('failed', 0) + 1
        return files
    
    def preload_target_files(self, folder_id: str, permit_no: str):
        """一次遞迴載入目標資料夾的所有檔名到記憶體 set。
        後續用 set lookup 取代逐檔 API 查詢。
        如果遞迴過程中任何一層失敗，不寫入快取，
        check_file_exists 會回退到逐檔 API 查詢。
        """
        file_set = set()
        if self._preload_recursive(folder_id, "", file_set):
            self._target_file_cache[permit_no] = file_set
        else:
            # 預載入不完整，不使用快取，強制走 API fallback
            self._target_file_cache.pop(permit_no, None)

    def _preload_recursive(self, folder_id: str, path: str, file_set: set) -> bool:
        """遞迴收集資料夾內所有檔案的 path/name。
        回傳 True 表示完整掃描成功，False 表示任一層失敗。
        """
        try:
            page_token = None
            while True:
                results = self._get_svc().files().list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields='nextPageToken, files(id, name, mimeType)',
                    pageSize=1000, pageToken=page_token,
                    supportsAllDrives=True, includeItemsFromAllDrives=True
                ).execute()
                for item in results.get('files', []):
                    item_path = f"{path}/{item['name']}" if path else item['name']
                    if item['mimeType'] == 'application/vnd.google-apps.folder':
                        self._subfolder_cache[(folder_id, item['name'])] = item['id']
                        if not self._preload_recursive(item['id'], item_path, file_set):
                            return False  # 子資料夾失敗，整體失敗
                    else:
                        file_set.add(item_path)
                page_token = results.get('nextPageToken')
                if not page_token:
                    break
            return True
        except HttpError as e:
            self._print(f"  ⚠️ 預載檔案快取失敗: {e}")
            return False

    def check_file_exists(self, folder_id: str, filename: str, path: str = "", permit_no: str = "") -> bool:
        """用預載入的 set 比對檔案是否存在（O(1) lookup，無 API 呼叫）"""
        if permit_no and permit_no in self._target_file_cache:
            key = f"{path}/{filename}" if path else filename
            # 同時檢查 .url 捷徑
            return key in self._target_file_cache[permit_no] or \
                   f"{key}.url" in self._target_file_cache[permit_no]
        # fallback：快取未載入時用 API 查詢
        target_folder_id = folder_id
        if path:
            target_folder_id = self.get_or_create_subfolder(folder_id, path)
            if not target_folder_id: return False
        try:
            safe_name = _escape_drive_query(filename)
            query = f"'{target_folder_id}' in parents and (name='{safe_name}' or name='{safe_name}.url') and trashed=false"
            results = self._get_svc().files().list(
                q=query, fields='files(id)', supportsAllDrives=True, includeItemsFromAllDrives=True
            ).execute()
            return len(results.get('files', [])) > 0
        except HttpError as e:
            self._print(f"  ⚠️ 檢查檔案存在失敗: {e}")
            return False

    def create_target_folder(self, folder_name: str) -> str:
        try:
            file_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [self.shared_drive_id]}
            folder = get_drive_service().files().create(body=file_metadata, fields='id', supportsAllDrives=True).execute()
            print(f"🆕 已自動建立資料夾: {folder_name}")
            return folder['id']
        except HttpError as e:
            print(f"⚠️ 建立資料夾失敗 {folder_name}: {e}")
            return None

    def get_or_create_subfolder(self, parent_id: str, path: str) -> str:
        current_folder_id = parent_id
        for folder_name in path.split('/'):
            if not folder_name: continue
            # 先查快取
            cache_key = (current_folder_id, folder_name)
            if cache_key in self._subfolder_cache:
                current_folder_id = self._subfolder_cache[cache_key]
                continue
            try:
                safe_folder = _escape_drive_query(folder_name)
                query = f"'{current_folder_id}' in parents and name='{safe_folder}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
                results = self._get_svc().files().list(q=query, fields='files(id)', supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
                items = results.get('files', [])
                if items:
                    current_folder_id = items[0]['id']
                else:
                    file_metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [current_folder_id]}
                    folder = self._get_svc().files().create(body=file_metadata, fields='id', supportsAllDrives=True).execute()
                    current_folder_id = folder['id']
                # 寫入快取
                self._subfolder_cache[cache_key] = current_folder_id
            except HttpError as e:
                self._print(f"  ⚠️ 子資料夾操作失敗 {folder_name}: {e}")
                return None
        return current_folder_id

    def create_shortcut_file(self, parent_id: str, filename: str, web_link: str):
        try:
            link_filename = f"{filename}.url"
            file_content = f"[InternetShortcut]\nURL={web_link}"
            file_metadata = {'name': link_filename, 'parents': [parent_id], 'mimeType': 'text/plain'}
            media = MediaIoBaseUpload(io.BytesIO(file_content.encode('utf-8')), mimetype='text/plain', resumable=True)
            self._get_svc().files().create(body=file_metadata, media_body=media, fields='id', supportsAllDrives=True).execute()
            return True
        except Exception as e:
            self._print(f"  ⚠️ 建立捷徑失敗 {filename}: {e}")
            return False

    def copy_file(self, source_file_id: str, target_folder_id: str, filename: str,
                  path: str = "", mime_type: str = PDF_MIME):
        """把來源檔複製進目標資料夾。`mime_type` 是**來源原值**。

        鏡像一律保留來源型別，不在這裡正規化（#116 review 第二輪）：
        正規化會把「副檔名與 mimeType 互相矛盾」這個訊號從資料裡抹掉，而下一輪
        重新掃描時我們只看得到鏡像裡的值。`監測資料.pdf` + `image/jpeg` 被寫成
        application/pdf 之後，下一輪就會被當成正常 PDF 送去解析。

        另外 `files().copy` 的主路徑本來就不帶 mimeType（Drive 保留來源值），
        下載回退路徑若自己正規化，同一個檔案會因為走哪條路而被標成不同型別——
        兩條路的結果必須一致。
        """
        final_folder_id = target_folder_id
        if path:
            final_folder_id = self.get_or_create_subfolder(target_folder_id, path)
            if not final_folder_id: return None, None

        svc = self._get_svc()
        try:
            file_metadata = {'name': filename, 'parents': [final_folder_id]}
            copied_file = svc.files().copy(fileId=source_file_id, body=file_metadata, fields='id', supportsAllDrives=True).execute()
            return copied_file['id'], final_folder_id
        except HttpError as e:
            try:
                request = svc.files().get_media(fileId=source_file_id, supportsAllDrives=True)
                file_buffer = io.BytesIO()
                downloader = MediaIoBaseDownload(file_buffer, request)
                done = False
                while not done: status, done = downloader.next_chunk()
                file_buffer.seek(0)
                # 型別跟著來源走。寫死 application/pdf 會把一張 jpg 標成 PDF 落進
                # 目標資料夾，而下游是以 mimeType 篩選的——那等於讓它混進上傳佇列。
                media = MediaIoBaseUpload(file_buffer, mimetype=mime_type, resumable=True)
                uploaded_file = svc.files().create(
                    body={'name': filename, 'parents': [final_folder_id],
                          'mimeType': mime_type},
                    media_body=media, fields='id', supportsAllDrives=True).execute()
                return uploaded_file['id'], final_folder_id
            except HttpError as download_error:
                if 'cannotDownloadFile' in str(download_error) or 'cannotCopyFile' in str(e):
                    return 'restricted', final_folder_id
                return None, None

    def _get_svc(self):
        """取得當前 thread 的 Drive service（並行時用 thread-local，序列時用全域）"""
        return get_thread_drive_service()

    def upload_remote_file(self, src, target_folder_id: str, adapter=None):
        """把非 Drive 來源的一個檔案下載後上傳進目標資料夾。

        與 copy_file 的差別只在來源：那邊是 Drive→Drive 的 files().copy，
        這邊沒有 source file id，只能下載位元組再 create。去重、子資料夾、
        目標結構全部共用同一套，不另開一條規則。
        """
        from geobingan_sync.source_adapters import fetch_pdf_bytes

        final_folder_id = target_folder_id
        if src.path:
            final_folder_id = self.get_or_create_subfolder(target_folder_id, src.path)
            if not final_folder_id:
                return None, None
        # 有些平台列檔時拿不到下載網址，要在**取檔前一刻**再換一次（換來的網址短效，
        # 提早換會讓大批次的後段全部過期）。沒有這個方法的 adapter 走原路。
        resolve = getattr(adapter, 'resolve_download', None) if adapter else None
        if resolve is not None:
            src = resolve(src)

        # allowed_hosts／allowed_host_suffixes 由 adapter 宣告並隨 SourceFile 傳進來：
        # 取檔時**每一跳**都要落在允許範圍內。兩者皆空會被 fetch_pdf_bytes 拒絕
        # （fail-closed），不會變成「沒宣告就等於不限制」。
        data = fetch_pdf_bytes(src.url, src.allowed_hosts,
                               allowed_host_suffixes=src.allowed_host_suffixes)
        return self.upload_bytes(src.name, data, final_folder_id), final_folder_id

    def upload_bytes(self, name: str, data: bytes, folder_id: str):
        """把位元組建成目標資料夾裡的 PDF。單檔與整包兩條路共用這一段。"""
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype='application/pdf', resumable=True)
        created = self._get_svc().files().create(
            body={'name': name, 'parents': [folder_id], 'mimeType': 'application/pdf'},
            media_body=media, fields='id', supportsAllDrives=True).execute()
        return created['id']

    def _sync_via_bundle(self, permit_no: str, source_url: str, target_folder_id: str, adapter):
        """整包來源的同步路徑：adapter 下載並解壓，我們逐檔去重後上傳。

        與逐檔路徑共用去重（check_file_exists）、子資料夾與 upload_bytes，只有取檔
        方式不同。一個檔案失敗不拖垮整案；整包失敗不拖垮整輪。
        """
        from geobingan_sync.source_adapters import AdapterError

        self.preload_target_files(target_folder_id, permit_no)
        copied = failed = seen = 0
        try:
            for src, data in adapter.fetch_all(source_url):
                seen += 1
                if self.check_file_exists(target_folder_id, src.name, src.path, permit_no):
                    continue
                try:
                    folder_id = target_folder_id
                    if src.path:
                        folder_id = self.get_or_create_subfolder(target_folder_id, src.path)
                        if not folder_id:
                            failed += 1
                            continue
                    self.upload_bytes(src.name, data, folder_id)
                except Exception as e:                      # noqa: BLE001
                    self._print(f"    ⚠️ 寫入目標資料夾失敗 {src.name}: {type(e).__name__}")
                    failed += 1
                    continue
                self._print(f"    🆕 發現新檔並上傳: {src.name}")
                copied += 1
                if permit_no in self._target_file_cache:
                    key = f"{src.path}/{src.name}" if src.path else src.name
                    self._target_file_cache[permit_no].add(key)
        except AdapterError as e:
            self._print(f"  ⚠️ 整包取得失敗: {e}")
            # fetch_all 是 generator：炸在第幾個成員決定了證據等級。已經吐出
            # 幾份才斷掉，`seen` 是下限不是份數（partial）；一開頭就取不到，
            # 就是什麼都沒量到（list_failed）。兩者都不可寫成數字份數。
            self._record_source_scan(permit_no, pdf=None, other=None,
                                     reason='partial' if seen else 'list_failed')
            self._record_error(permit_no, f'{adapter.name}:{e}')
            self.save_state()
            return

        if seen:
            self._print(f"  📊 整包 {seen} 份，新增 {copied} 個"
                        + (f"、失敗 {failed} 個" if failed else ""))
        # 整包解完才知道來源有幾份，所以量測寫在這裡。逐檔寫入目標失敗是**同步**
        # 問題不是量測問題（那條已經進 _record_error），份數本身仍然是定論。
        # other=None：adapter 在解壓時就只收 .pdf，非 PDF 的數量它沒回報給我們。
        #
        # seen==0 不可寫成實測 0：整包來源有取用間隔（dropbox 每 7 天一次），
        # 未到間隔時 fetch_all 什麼都不吐、而且那**不是錯誤**。從這裡看不出
        # 「刻意跳過」與「整包真的沒有 PDF」的差別，照 fail-open 記成未取得。
        # 要分得開就得讓整包協定回報跳過，那是另一件事（已記在 architecture.md）。
        if seen:
            self._record_source_scan(permit_no, pdf=seen, other=None, reason='ok')
        else:
            self._record_source_scan(permit_no, pdf=None, other=None,
                                     reason='not_measured')
        with self._state_lock:
            self.copied_total += copied
            self.adapter_uploaded += copied
            self.adapter_failed += failed
            if copied:
                self.permits_with_new += 1
            self.state['processed'][permit_no] = True
        if failed:
            self._record_error(permit_no, f'{adapter.name}:{failed} 個檔案寫入失敗')
        self.save_state()

    def _sync_via_adapter(self, permit_no: str, source_url: str, target_folder_id: str, adapter):
        """非 Drive 來源的同步路徑。

        一個檔案失敗不可拖垮整案，一案失敗不可拖垮整輪——這條路走的是外部網站，
        逾時與格式異常是常態不是例外。
        """
        from geobingan_sync.source_adapters import AdapterError

        self._print(f"  🔌 來源類型: {adapter.name}")
        if hasattr(adapter, 'fetch_all'):
            # 整包來源：平台沒有便宜的列檔方式，只能一次下載整包再解壓。
            # 走自己的路而不是硬塞進 list_files + 逐檔取檔——那會為每個檔案
            # 重複下載整包（實測最大一包 284MB）。
            return self._sync_via_bundle(permit_no, source_url, target_folder_id, adapter)
        try:
            files = adapter.list_files(source_url)
        except AdapterError as e:
            self._print(f"  ⚠️ 來源列檔失敗: {e}")
            self._record_source_scan(permit_no, pdf=None, other=None,
                                     reason='list_failed')
            self._record_error(permit_no, f'{adapter.name}:{e}')
            self.save_state()
            return

        # 清單完整取得的這一刻份數就確定了，所以量測寫在下載迴圈**之前**：
        # 「來源有什麼」不該被後面逐檔取檔的成敗改寫。
        # other=None：adapter 列檔時就把非 PDF 過濾掉了（見各 adapter 的 .pdf
        # 檢查），那個數字沒量到，不可當成 0。
        self._record_source_scan(permit_no, pdf=len(files), other=None,
                                 reason='ok' if files else 'empty')
        self.preload_target_files(target_folder_id, permit_no)
        copied = failed = 0
        for src in files:
            if self.check_file_exists(target_folder_id, src.name, src.path, permit_no):
                continue
            try:
                file_id, _ = self.upload_remote_file(src, target_folder_id, adapter=adapter)
            except AdapterError as e:
                self._print(f"    ⚠️ 取檔失敗 {src.name}: {e}")
                failed += 1
                continue
            except Exception as e:                          # noqa: BLE001
                # 刻意不用「上傳失敗」字樣：那在本專案語彙裡指 geoBingAn 後端上傳，
                # 兩件事混在一起會讓操作者查錯方向（舊 shell 還曾用它 grep 計數）
                self._print(f"    ⚠️ 寫入目標資料夾失敗 {src.name}: {type(e).__name__}")
                failed += 1
                continue
            if file_id:
                self._print(f"    🆕 發現新檔並上傳: {src.name}")
                copied += 1
                if permit_no in self._target_file_cache:
                    key = f"{src.path}/{src.name}" if src.path else src.name
                    self._target_file_cache[permit_no].add(key)

        if copied or failed:
            self._print(f"  📊 更新完成: 新增 {copied} 個" + (f"、失敗 {failed} 個" if failed else ""))
        with self._state_lock:
            self.copied_total += copied
            self.adapter_uploaded += copied
            self.adapter_failed += failed
            if copied:
                self.permits_with_new += 1
            self.state['processed'][permit_no] = True
        if failed:
            self._record_error(permit_no, f'{adapter.name}:{failed} 個檔案取得失敗')
        self.save_state()

    def sync_permit(self, permit_no: str, source_url: str, target_folder_id: str):
        self._print(f"\n🔄 監測建案: {permit_no}")
        self._mark_visited(permit_no)
        source_folder_id = self.extract_folder_id_from_url(source_url)
        if not source_folder_id:
            from geobingan_sync.source_adapters import find_adapter
            adapter = find_adapter(source_url)
            if adapter:
                return self._sync_via_adapter(permit_no, source_url, target_folder_id, adapter)
            self._record_error(permit_no, 'Invalid URL ID')
            self._record_source_scan(permit_no, pdf=None, other=0, reason='invalid_url')
            return

        try:
            scan = {'skipped': 0, 'failed': 0}
            files = self.list_files_recursive(source_folder_id, scan=scan)
            # 列出失敗就是「本輪清單不完整」，和抓到幾份無關，所以在這裡一次記完，
            # 不留給下面兩條分支各自自律。partial 那條原本就漏了（#114 review P1）：
            # 資料層寫了 pdf=None，連續失敗層卻把它結算成「這輪成功」並清掉既有
            # streak，於是同一個子資料夾天天 403 也永遠到不了三輪告警——和 #111
            # 修掉的是同一個錯誤類型（兩層對同一輪得到相反結論）。
            if scan['failed']:
                self._record_error(permit_no, f"來源列出失敗（{scan['failed']} 個資料夾）")
            # PDF 與非 PDF 分開數。兩者都鏡像，但只有 PDF 會上傳到 riskmap，
            # 所以 source_scan 的 `pdf` 必須只算 PDF——把 13 個 .xls 算進 `pdf`
            # 就等於宣稱這案有 13 份報告在收，正是這個欄位要防的誤讀。
            # 用共用判定分類，不要在這裡自己比 mimeType：`報告.pdf` 標成
            # octet-stream 要算 PDF、`監測資料.jpg` 標成 application/pdf 不算。
            pdf_files = [f for f in files if is_uploadable_pdf(f[1], f[4])]
            other_files = [f for f in files if not is_uploadable_pdf(f[1], f[4])]
            # 量測只在**一個地方**落檔（迴圈之後），不在這裡先記一次：
            # 「沒有 PDF 但有圖片可鏡像」現在會繼續走複製迴圈，兩處各記一次就會
            # 互相覆寫，而且後寫的那次 reason 是 'ok'（它只看 len(files)）。
            if not pdf_files and not other_files:
                if scan['failed']:
                    self._print(f"  ⚠️ 來源列出失敗 {scan['failed']} 個資料夾，本輪未量到（不記為 0 份）")
                elif scan['skipped']:
                    self._print(f"  ⚠️ 來源有 {scan['skipped']} 個不收的檔案型別，沒有可鏡像的檔案")
                else:
                    # 零筆有兩種完全不同的成因，在這裡問清楚（見
                    # `_source_folder_state`），不要讓「資料夾不見了」被寫成
                    # 實測 0 份。
                    state = self._source_folder_state(source_folder_id)
                    if state == 'gone':
                        scan['missing'] = True
                        self._print("  ⚠️ 來源資料夾已不存在或被丟進垃圾桶，"
                                    "本輪未量到（不記為 0 份）")
                    elif state == 'unknown':
                        # 查不到就是未取得。這條會進錯誤帳：連續查不到要讓
                        # streak 告警看得見，而「不存在」是狀態不是失敗，不進。
                        scan['failed'] = scan.get('failed', 0) + 1
                        self._record_error(permit_no, '來源資料夾狀態查不到')
                        self._print("  ⚠️ 來源資料夾狀態查不到，"
                                    "本輪未量到（不記為 0 份）")
                    else:
                        self._print(f"  ⚠️ 來源無檔案")
                self._record_source_scan(permit_no, **self._scan_result(scan, [], []))
                return
            if not pdf_files:
                self._print(f"  ⚠️ 來源有 {len(other_files)} 個非 PDF 檔但沒有 PDF"
                            f"（會鏡像進雲端硬碟，但不會上傳到 riskmap）")

            # 預載入目標資料夾的完整檔案樹（1 次遞迴 vs 逐檔 API 查詢）
            self.preload_target_files(target_folder_id, permit_no)

            copied = 0

            for file_id, filename, path, web_link, mime_type in files:
                display_path = f"{path}/{filename}" if path else filename

                if self.check_file_exists(target_folder_id, filename, path, permit_no):
                    continue

                result_id, final_folder_id = self.copy_file(
                    file_id, target_folder_id, filename, path,
                    mime_type=mime_type or PDF_MIME)
                if result_id == 'restricted':
                    self._print(f"    🔒 受限: {display_path} (建立捷徑)")
                    self.create_shortcut_file(final_folder_id, filename, web_link)
                    with self._state_lock:
                        self.restricted_files.append({'filename': filename, 'permit': permit_no})
                    if permit_no in self._target_file_cache:
                        key = f"{path}/{filename}.url" if path else f"{filename}.url"
                        self._target_file_cache[permit_no].add(key)
                elif result_id:
                    self._print(f"    🆕 發現新檔並複製: {display_path}")
                    copied += 1
                    if permit_no in self._target_file_cache:
                        key = f"{path}/{filename}" if path else filename
                        self._target_file_cache[permit_no].add(key)

            if copied > 0:
                self._print(f"  📊 更新完成: 新增 {copied} 個")
            with self._state_lock:
                self.copied_total += copied
                if copied:
                    self.permits_with_new += 1

            # 成功路徑也要落檔，否則 source_scan 只剩失敗樣本，分不出「這案沒量過」
            # 與「這案量到有檔案」。scan['failed'] 非 0 時份數是不完整的下限，
            # 照 fail-open 記成未取得（錯誤已在列檔後就記過，不在這裡重複記）。
            if scan['failed']:
                self._print(f"  ⚠️ 來源有 {scan['failed']} 個資料夾列不出來，"
                            f"這 {len(pdf_files)} 份 PDF 只是下限（本輪不記為確定份數）")
            self._record_source_scan(permit_no,
                                     **self._scan_result(scan, pdf_files, other_files))
            with self._state_lock:
                self.state['processed'][permit_no] = True
            self.save_state()

        except Exception as e:
            self._print(f"  ❌ 處理中斷: {e}")
            self._record_error(permit_no, e)
            self.save_state()
    
    def _resolve_or_skip_indirect(self, mapping: Dict[str, str], resolver=None,
                                  cache_path: str = None,
                                  unsupported_path: str = None) -> Dict[str, str]:
        """把非 Drive 來源解析成 Drive 資料夾；解不出來的**剔除**，不進同步流程。

        剔除而非保留的理由：保留只會讓 run() 建出空的目標資料夾、再記一筆同步錯誤，
        對操作者是雜訊，對資料是零收益。解不開的那些由 health_check 的來源資料夾
        檢查與 registry 繼續追蹤（match_permits 仍會看到完整清單）。

        解析失敗不可中斷同步：任何例外都只記錄、該案剔除、其餘照跑。
        """
        from geobingan_sync.source_adapters import find_adapter

        direct, indirect, adapted = {}, [], {}
        for permit, url in mapping.items():
            if self.extract_folder_id_from_url(url):
                direct[permit] = url
            elif find_adapter(url):
                # 有 adapter 的維持原始 URL 進同步，由 sync_permit 分流；
                # 不必再花一次網路請求去解析成 Drive（它本來就不在 Drive）。
                adapted[permit] = url
            else:
                indirect.append((permit, url))
        if adapted:
            print(f"  🔌 非 Drive 來源 {len(adapted)} 案由 adapter 直接處理")
            direct.update(adapted)
        if not indirect:
            # 仍要寫一次空名單：全部來源都接得到時，名單必須縮成空的。
            # 早退跳過這一步的話，舊名單會永遠留著，變成只增不減的舊帳。
            self._record_unsupported([], unsupported_path)
            return direct

        from geobingan_sync.link_resolver import (cached_resolve, load_cache, save_cache,
                                                  CACHE_FILE)
        path = cache_path or CACHE_FILE
        try:
            cache = load_cache(path)
        except Exception:                                   # noqa: BLE001
            cache = {}
        rescued, unsupported = 0, []
        for permit, url in indirect:
            try:
                res = cached_resolve(permit, url, cache, resolver=resolver)
            except Exception as e:                          # noqa: BLE001
                print(f"  ⚠️ 連結解析失敗 {permit}: {type(e).__name__}")
                unsupported.append((permit, url, f'resolve_error:{type(e).__name__}'))
                continue
            if res.ok:
                direct[permit] = f'https://drive.google.com/drive/folders/{res.folder_id}'
                rescued += 1
            else:
                unsupported.append((permit, url, res.note or 'unresolved'))
        skipped = len(unsupported)
        try:
            save_cache(cache, path)
        except Exception as e:                              # noqa: BLE001
            print(f"  ⚠️ 連結解析快取寫入失敗: {type(e).__name__}")
        print(f"  🔗 間接連結：解析成功 {rescued} 案納入同步、{skipped} 案非 Drive 空間暫不支援（已跳過，不建空資料夾）")
        self._record_unsupported(unsupported, unsupported_path)
        return direct

    def _record_unsupported(self, entries, path: str = None):
        """把本輪接不到的來源寫成名單。

        被剔除的建案原本在系統裡完全消失——沒有名單、沒有原因、沒有時間，要回答
        「哪些建案拿不到資料、為什麼」只能重跑一輪人工探測。缺口大部分不是工程
        問題（承造人給的連結需要帳號），要走對外溝通，而對外溝通需要一份維護中
        的名單。

        以本輪的集合重寫（不是累加）：建案換了連結或我們補上 adapter，就要從名單
        消失，否則會變成只增不減的舊帳。寫入失敗不可中斷同步。
        """
        from geobingan_sync import unsupported_sources as us
        try:
            data = us.build(entries, previous=us.load(path))
            # 對到期的案子重測一次。手寫的「需登入」結論會過期——承造人改好了權限
            # 我們不會知道（同 #100 失效資料夾永不重驗的那個錯）。恢復要出聲。
            try:
                probed, recovered = us.probe_due(data)
                if probed:
                    print(f"     🔁 重測 {probed} 案接不到的來源")
                if recovered:
                    print(f"     🟢 先前打不開的來源現在有回應了（{len(recovered)} 案）："
                          f"{'、'.join(recovered[:5])}"
                          + (' 等' if len(recovered) > 5 else '')
                          + "——請確認是否已可取得資料")
            except Exception as e:                            # noqa: BLE001
                # 重測是加值資訊，失敗不可影響名單本身
                print(f"  ⚠️ 來源重測失敗（名單仍會更新）: {type(e).__name__}")
            us.save(data, path)
            for fam, n, status, date, _note in us.summarise(data)[:4]:
                print(f"     · {fam} {n} 案（{status}，{date} 探測）")
        except Exception as e:                              # noqa: BLE001
            print(f"  ⚠️ 未支援來源名單寫入失敗: {type(e).__name__}")

    def run(self):
        """包一層 try/finally：**結算必須在 run() 拋錯時也跑到**。

        2026-10-06 實跑踩到：掃描共享雲端逾時 → run() 拋錯 → 城市層（main 的
        迴圈）只 print 不重拋 → 結算整段跳過，搬遷沒跑、last_run 沒寫。

        但光搬進 finally 會製造假綠：那輪走訪 0 案，last_run 會是
        {visited: 0, errored: 0}，讀起來就是「0 案全部無錯誤」的綠燈。所以同時
        把「這輪有沒有跑完」與 run 層級的錯誤訊息一起記下去——沒跑完的輪次不可
        被當成乾淨的觀測（同 #108 的教訓：放進 finally 不可把失敗記成成功）。
        """
        completed = False
        try:
            self._run_steps()
            completed = True
        except Exception as e:
            # 城市層只 print 不重拋，所以訊息要在這裡留下來，否則資料裡
            # 完全看不出「為什麼沒結算」
            self._run_error = f'{type(e).__name__}: {e}'
            raise
        finally:
            self._finalize_errors(completed=completed)

    def _run_steps(self):
        print("="*70)
        print(f"🚀 建築執照監測資料同步工具 v5.1 ({self.city_name})")
        print("   特性: 增量同步、跳過已處理建案、快速模式")
        print("="*70)

        if self.source_type == 'csv':
            self.permit_mapping = self.load_csv_list()
        else:
            pdf_path = self.download_pdf_list()
            self.permit_mapping = self.parse_pdf_list(pdf_path)
            self._record_list_fingerprint()
        self.target_folders = self.scan_shared_drive()

        # 解析間接連結**必須在建立目標資料夾之前**（review P1）。
        # parse_pdf_list 現在抓任何 http(s)，主同步因此會收到 74 個非 Drive 網址。
        # 若不先處理，run() 會為每一個都先建好 Shared Drive 目標資料夾，之後才在
        # sync_permit 因 Invalid URL ID 失敗 —— 留下一堆空資料夾與同步錯誤紀錄，
        # 而 resolver 救得回來的 27 案也還是沒被同步到。
        self.permit_mapping = self._resolve_or_skip_indirect(self.permit_mapping)

        permit_list = list(self.permit_mapping.items())
        # 隨機打亂，確保每次執行檢查不同建案
        random.shuffle(permit_list)

        # 不再用 state['processed'] 永久跳過已處理建照 — 那會錯過承造人後續在
        # personal Drive 加的新檔（2026-05-05 揚昇君悅 case：4/7 後 4 週新檔
        # 全部沒同步到 Shared Drive，因為被當「processed 跳過」）。
        # 改成每次都 revisit 全部 permit；逐檔 dedup 由 check_file_exists 處理。
        unprocessed_permits = list(permit_list)

        print(f"\n📋 監測目標: {len(permit_list)} 個建案（全部 revisit）")
        print(f"🔄 待處理: {len(unprocessed_permits)} 個")

        # 先確保所有建案都有目標資料夾（序列化，因為涉及建立資料夾）
        permits_with_targets = []
        for permit_no, source_url in unprocessed_permits:
            if permit_no in self.target_folders:
                target_id = self.target_folders[permit_no]
            else:
                print(f"\n🔧 發現新建案: {permit_no}")
                target_id = self.create_target_folder(permit_no)
                if target_id:
                    self.target_folders[permit_no] = target_id
                else:
                    continue
            permits_with_targets.append((permit_no, source_url, target_id))

        # 並行處理各建案（每個建案的來源/目標資料夾互相獨立）
        if len(permits_with_targets) > 1:
            print(f"\n⚡ 並行處理（{MAX_CONCURRENT_PERMITS} 執行緒）")
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PERMITS) as executor:
            futures = {
                executor.submit(self.sync_permit, pn, url, tid): pn
                for pn, url, tid in permits_with_targets
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    permit_no = futures[future]
                    self._print(f"  ❌ {permit_no} 未預期錯誤: {e}")
                    # 原本只 print 不落檔 → 這類錯誤從來沒進過資料，也就永遠
                    # 不會被連續失敗計數或告警看到
                    self._record_error(permit_no, f'未預期錯誤: {e}')

        self._write_step_result()

    def _finalize_errors(self, completed: bool = True):
        """結算本輪的連續失敗計數、搬遷舊資料、修剪保留窗。

        順序是刻意的：**先搬遷再修剪**。那 2,067 筆沒有時間戳的舊項目裡仍埋著
        「某案從以前就一直失敗」，先修剪會在新計數器累積到結論（3 輪）之前就
        失去那段歷史，等於自己製造一段偵測空窗。

        失敗不可中斷同步（它是觀測，不是同步本身），但也不可無聲。
        """
        try:
            moved = sync_errors.migrate_legacy(self.state)
            if moved:
                print(f"  📦 舊錯誤紀錄 {moved} 筆（無時間戳）已搬入 errors_legacy")
            res = sync_errors.finalize_run(
                self.state, self._visited_permits, self._errored_permits,
                run=os.environ.get('SYNC_RUN_STARTED') or None,
                completed=completed, run_error=self._run_error)
            dropped = sync_errors.trim_errors(self.state)
            self.save_state()

            if not completed:
                print(f"  🛑 本輪未跑完（{self._run_error or '原因未記錄'}）"
                      f"——已走訪 {res['visited']} 案，這輪不計為乾淨觀測")
            if res['errored'] and res['visited']:
                print(f"  ⚠️ 本輪 {res['errored']} / {res['visited']} 案出錯"
                      f"（{res['errored'] / res['visited']:.1%}）")
            if res['newly_stuck']:
                print(f"  🔴 連續 {sync_errors.ERROR_STREAK_ALERT} 輪失敗"
                      f"（{len(res['newly_stuck'])} 案）: "
                      + '、'.join(res['newly_stuck'][:5])
                      + (' 等' if len(res['newly_stuck']) > 5 else ''))
            if res['recovered']:
                print(f"  🟢 先前失敗的案這輪成功了（{len(res['recovered'])} 案）: "
                      + '、'.join(res['recovered'][:5])
                      + (' 等' if len(res['recovered']) > 5 else ''))
            # 寫成資料但沒人看，和只印在日誌一樣是空窗。這輪量到「來源有東西
            # 卻沒有 PDF」的案要在摘要露出——那是需要人決定要不要支援的訊號，
            # 不是錯誤，所以不進 error streak，但也不能只躺在 state 裡。
            scanned = self.state.get('source_scan') or {}
            other_only = sorted(p for p, v in scanned.items()
                                if p in self._visited_permits
                                and (v or {}).get('reason') == 'no_pdf_but_other')
            if other_only:
                print(f"  📁 來源有檔案但沒有 PDF（{len(other_only)} 案）: "
                      + '、'.join(other_only[:5])
                      + (' 等' if len(other_only) > 5 else '')
                      + "——承造人改用 .xls／圖片發布，已鏡像進雲端硬碟，"
                        "但不會上傳到 riskmap")
            gone = sorted(p for p, v in scanned.items()
                           if p in self._visited_permits
                           and (v or {}).get('reason') == 'source_gone')
            if gone:
                print(f"  🔗 來源資料夾已不存在（{len(gone)} 案）: "
                      + '、'.join(gone[:5])
                      + (' 等' if len(gone) > 5 else '')
                      + "——帳上記未取得而不是 0 份；要不要請承造人重新提供"
                        "由人決定")
            if dropped:
                print(f"  🧹 錯誤紀錄修剪 {dropped} 筆"
                      f"（保留 {sync_errors.KEEP_ERROR_DAYS} 天／"
                      f"上限 {sync_errors.MAX_ERRORS} 筆；計數器不受影響）")
        except Exception as e:                                  # noqa: BLE001
            print(f"  ⚠️ 錯誤紀錄結算失敗（同步本身不受影響）: {type(e).__name__}")

    def _write_step_result(self):
        """把本輪的數量寫成資料，給 record_sync_result 讀。

        以前是 shell 用 grep 從日誌反推，樣式跟訊息漂開之後就靜默回 0——累計
        119 次執行的 total_synced_pdfs 一直是 0，而 9/30 那輪實際新增 2,499 份。
        數字由**產生它的這一步**寫出來，消費端不必猜我們印了什麼字。

        寫入失敗不可中斷同步：它是統計，不是同步本身。但也不可無聲，所以印出來。
        """
        from geobingan_sync import step_results
        try:
            # accumulate 而非 write：run() 是每個城市跑一次，直接覆蓋會讓
            # 後一個城市蓋掉前一個的數字（目前只啟用台北市，那是剛好沒事）
            step_results.accumulate(step_results.SYNC, {
                'synced': self.copied_total,
                'permits_with_new': self.permits_with_new,
                'errors': len(self._errored_permits),
                'adapter_uploaded': self.adapter_uploaded,
                'adapter_failed': self.adapter_failed,
            })
            print(f"\n📊 本輪新增 {self.copied_total} 份（{self.permits_with_new} 個建案）"
                  + (f"，其中 adapter 上傳 {self.adapter_uploaded} 份" if self.adapter_uploaded else ""))
        except Exception as e:                              # noqa: BLE001
            print(f"⚠️ 同步結果數量寫入失敗: {type(e).__name__}——本輪計數將顯示為未取得")


def main(argv=None):
    """CLI 進入點。抽成函式才測得到「城市失敗 → 結束碼非零」這段行為。

    （原本整段寫在 `if __name__ == '__main__':` 裡，無法從測試觸發。）
    """
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--city', default=None, help='City ID or "all"')
    args = parser.parse_args(argv)

    cities = get_cities_for_cli(args.city)
    failed = []
    for city in cities:
        print(f"\n{'='*70}")
        print(f"🏙️  處理城市: {city['name']}")
        print(f"{'='*70}")
        try:
            sync = PermitSync(city=city)
            sync.run()
        except KeyboardInterrupt:
            print("\n🛑 使用者手動停止")
            break
        except Exception as e:                              # noqa: BLE001
            print(f"\n❌ {city['name']} 發生錯誤: {e}")
            failed.append(city['name'])
    if failed:
        # 原本這裡只 print，main 正常返回 → process **exit 0** → shell 的
        # `if ! python3 -m ...` 不觸發、STEP1_FAILED 保持 0，步驟 2-3 照跑、
        # handle_error 也不會記。2026-10-06 掃描共享雲端逾時就是這樣被當成
        # 成功的（當天 sync_status 沒有任何失敗紀錄）。
        # 一個城市失敗不中斷其他城市，但整體結束碼必須反映失敗。
        print(f"\n🛑 {len(failed)} 個城市同步失敗: {'、'.join(failed)}")
        sys.exit(1)


if __name__ == '__main__':
    main()
