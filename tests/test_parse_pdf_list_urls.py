"""清單解析要抓**任何** http(s) 連結，並把 folder id 的抽取限定在 Drive 主機。

2026-09-29 發現：舊版只抓 `https://drive.google.com` 開頭，於是 Google Sites／
SharePoint／gofile／Dropbox／Synology 等 20 幾種空間在這一步就被當成「無連結」
丟掉（實測 439 案中 71 案）。PR #95 的 link_resolver 因此只收到 2 個候選而不是
73 個，整個功能等於白做——**模組測過了，但正式管線沒餵到料**。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.steps.sync_permits import PermitSync


@pytest.fixture
def ps():
    return PermitSync(city={'name': 'T', 'pdf_list_url': 'https://x.test/l.pdf'})


# ---------- folder id 只認 Drive 主機 ----------

@pytest.mark.parametrize('url,expected', [
    ('https://drive.google.com/drive/folders/1N07lVUXyZH7ZjzAaZfAb', '1N07lVUXyZH7ZjzAaZfAb'),
    ('https://drive.google.com/drive/u/1/folders/13F89BOn5XfWGjZ-', '13F89BOn5XfWGjZ-'),
    ('https://drive.google.com/drive/mobile/folders/1v_Z4g70VEI7-', '1v_Z4g70VEI7-'),
    ('https://drive.google.com/open?id=1AbCdEfGh', '1AbCdEfGh'),
    ('https://docs.google.com/folders/1XyZ', '1XyZ'),
])
def test_drive_urls_yield_folder_id(ps, url, expected):
    assert ps.extract_folder_id_from_url(url) == expected


@pytest.mark.parametrize('url', [
    'https://example.com/share?oid=AbC123XyZ',          # oid= 也含 id=，舊版會誤抓
    'https://foo.sharepoint.com/x?id=DocLib42',
    'https://nas.example.com:5000/sharing/abc?id=share01',
    'https://sites.google.com/view/duw231107016',
    'http://gofile.me/3nCZT/Dg4yzeJWI',
    'https://www.dropbox.com/scl/fo/abc/h?rlkey=xyz&dl=0',
    'https://mega.nz/folder/SQEQVBpK#z3XxyS9',
])
def test_non_drive_urls_yield_no_folder_id(ps, url):
    """拿假的 folder id 去查 Drive，查不到還算好，查到別人的資料夾更糟。"""
    assert ps.extract_folder_id_from_url(url) is None


def test_empty_and_none_are_safe(ps):
    assert ps.extract_folder_id_from_url('') is None
    assert ps.extract_folder_id_from_url(None) is None


def test_lookalike_host_is_rejected(ps):
    """`drive.google.com.evil.test` 不是 Drive。"""
    assert ps.extract_folder_id_from_url(
        'https://drive.google.com.evil.test/drive/folders/1AbC') is None


# ---------- 清單解析抓任何 http(s) ----------

class _FakePage:
    def __init__(self, text):
        self._t = text

    def extract_text(self):
        return self._t


def _parse(ps, monkeypatch, text, tmp_path):
    import geobingan_sync.steps.sync_permits as sp

    class _Reader:
        def __init__(self, f):
            self.pages = [_FakePage(text)]
    monkeypatch.setattr(sp.pypdf, 'PdfReader', _Reader)
    f = tmp_path / 'l.pdf'
    f.write_bytes(b'%PDF-1.4')
    return ps.parse_pdf_list(str(f))


def test_sharepoint_colon_not_truncated(ps, monkeypatch, tmp_path):
    """少了 `:` 的話 `/:f:/g/...` 會在第一個冒號就被切斷。"""
    url = 'https://cectw-my.sharepoint.com/:f:/g/personal/220g_cectw_com/EttQ4E'
    out = _parse(ps, monkeypatch, f'111建字第0017號 王建築師事務所 大陸工程 {url} 下一欄中文', tmp_path)
    assert out['111建字第0017號'] == url


@pytest.mark.parametrize('url', [
    'https://sites.google.com/view/duw231107016',
    'http://gofile.me/3nCZT/Dg4yzeJWI',
    'https://www.dropbox.com/scl/fo/abc/h?rlkey=xyz&dl=0',
    'https://mega.nz/folder/SQEQVBpK#z3XxyS9',
    'https://1drv.ms/f/s!AbCdEf',
    'http://125.227.22.67:5000/sharing/rW7E3kLc5',
    'https://e.pcloud.link/publink/show?code=kZabc',
])
def test_non_drive_hosts_are_captured(ps, monkeypatch, tmp_path, url):
    """舊版把這些全丟掉，下游 resolver 因此收不到料。"""
    out = _parse(ps, monkeypatch, f'112建字第0001號 事務所 營造 {url} 中文結尾', tmp_path)
    assert out.get('112建字第0001號') == url


def test_chinese_terminates_the_url(ps, monkeypatch, tmp_path):
    """空白已被移除，邊界只能靠中文字截斷。"""
    out = _parse(ps, monkeypatch, '113建字第0015號 某事務所 某營造 https://x.test/a/b 監測資料夾', tmp_path)
    assert out['113建字第0015號'] == 'https://x.test/a/b'


def test_first_url_in_chunk_wins(ps, monkeypatch, tmp_path):
    out = _parse(ps, monkeypatch,
                 '114建字第0032號 甲 乙 https://first.test/a 中文 https://second.test/b', tmp_path)
    assert out['114建字第0032號'] == 'https://first.test/a'


def test_permit_without_any_url_is_counted_missing(ps, monkeypatch, tmp_path):
    out = _parse(ps, monkeypatch, '110建字第0001號 甲事務所 乙營造 尚未提供', tmp_path)
    assert '110建字第0001號' not in out


def test_two_permits_each_get_their_own_url(ps, monkeypatch, tmp_path):
    text = ('111建字第0100號 甲 乙 https://a.test/one 中文 '
            '111建字第0200號 丙 丁 https://b.test/two 中文')
    out = _parse(ps, monkeypatch, text, tmp_path)
    assert out['111建字第0100號'] == 'https://a.test/one'
    assert out['111建字第0200號'] == 'https://b.test/two'


# ---------- run() 層級分流（review P1）----------
#
# parse_pdf_list 有兩個 consumer：match_permits 需要全部 URL（交 resolver），
# 但 PermitSync.run() 假設來源能直接抽出 Drive folder id。放寬抓取後主同步會
# 收到 74 個非 Drive URL，若不先分流，run() 會為每一個先建好 Shared Drive 目標
# 資料夾、之後才在 sync_permit 因 Invalid URL ID 失敗——留下空資料夾與錯誤紀錄。

from geobingan_sync.link_resolver import Resolution

FOLDER = 'https://drive.google.com/drive/folders/1N07lVUXyZH7ZjzAaZfAb'
FID = '1N07lVUXyZH7ZjzAaZfAb'


def test_indirect_link_resolved_before_target_creation(ps, tmp_path):
    """能救回的要變成 Drive 網址納入同步——這才是這串修改的目的。"""
    out = ps._resolve_or_skip_indirect(
        {'A': 'https://sites.google.com/view/x'},
        resolver=lambda u: Resolution(FID, 'embedded'),
        cache_path=str(tmp_path / 'c.json'))
    assert out['A'] == FOLDER
    assert ps.extract_folder_id_from_url(out['A']) == FID


def test_unresolvable_indirect_link_is_dropped(ps, tmp_path):
    """解不開的要剔除，否則 run() 會建出空資料夾再記一筆同步錯誤。"""
    out = ps._resolve_or_skip_indirect(
        {'A': 'https://cectw-my.sharepoint.com/:f:/g/personal/x'},
        resolver=lambda u: Resolution(None, 'none', 'no_drive_link'),
        cache_path=str(tmp_path / 'c.json'))
    assert 'A' not in out


def test_direct_drive_urls_pass_through_without_resolving(ps, tmp_path):
    called = []
    out = ps._resolve_or_skip_indirect(
        {'A': FOLDER}, resolver=lambda u: called.append(u) or Resolution(None, 'none'),
        cache_path=str(tmp_path / 'c.json'))
    assert out == {'A': FOLDER} and called == [], '已是 Drive 就不該再解析'


def test_mixed_mapping_keeps_direct_and_rescued_only(ps, tmp_path):
    mapping = {
        'DIRECT': FOLDER,
        'RESCUE': 'https://sites.google.com/view/x',
        'DROP': 'http://gofile.me/abc',
    }
    def res(u):
        return Resolution(FID, 'embedded') if 'sites.google' in u else Resolution(None, 'none')
    out = ps._resolve_or_skip_indirect(mapping, resolver=res, cache_path=str(tmp_path / 'c.json'))
    assert set(out) == {'DIRECT', 'RESCUE'}
    # 每一筆留下來的都必須能抽出 folder id，run() 才不會建空資料夾
    assert all(ps.extract_folder_id_from_url(u) for u in out.values())


def test_resolver_exception_drops_that_permit_only(ps, tmp_path):
    """解析失敗不可中斷同步：該案剔除、其餘照跑。"""
    def boom(u):
        if 'bad' in u:
            raise RuntimeError('x')
        return Resolution(FID, 'embedded')
    out = ps._resolve_or_skip_indirect(
        {'BAD': 'https://bad.test/a', 'OK': 'https://sites.google.com/view/x', 'D': FOLDER},
        resolver=boom, cache_path=str(tmp_path / 'c.json'))
    assert set(out) == {'OK', 'D'}


def test_no_indirect_links_is_a_noop(ps, tmp_path):
    mapping = {'A': FOLDER, 'B': FOLDER}
    assert ps._resolve_or_skip_indirect(mapping, cache_path=str(tmp_path / 'c.json')) == mapping


def test_run_resolves_before_creating_target_folders(ps, monkeypatch, tmp_path):
    """行為面：非 Drive 的建案不得走到 create_target_folder。"""
    created = []
    synced = []
    monkeypatch.setattr(ps, 'download_pdf_list', lambda *a, **k: str(tmp_path / 'l.pdf'))
    monkeypatch.setattr(ps, 'parse_pdf_list', lambda p: {
        'DIRECT': FOLDER,
        'RESCUE': 'https://sites.google.com/view/x',
        'DROP': 'http://gofile.me/abc',
    })
    monkeypatch.setattr(ps, '_record_list_fingerprint', lambda *a, **k: None)
    monkeypatch.setattr(ps, 'scan_shared_drive', lambda *a, **k: {})
    monkeypatch.setattr(ps, 'create_target_folder',
                        lambda permit: created.append(permit) or f'tgt-{permit}')
    monkeypatch.setattr(ps, 'sync_permit', lambda pn, url, tid: synced.append((pn, url)) or {})
    monkeypatch.setattr(ps, '_resolve_or_skip_indirect',
                        lambda m, **kw: {k: (FOLDER if k != 'DROP' else None) for k in m
                                         if k != 'DROP'})
    try:
        ps.run()
    except Exception:
        pass                      # run() 尾段還有統計／狀態寫入，這裡只驗前半段行為
    assert 'DROP' not in created, '非 Drive 的建案不該被建立目標資料夾'
    assert 'DROP' not in [p for p, _ in synced], '非 Drive 的建案不該進同步'


# ---------- 頁碼不得混進網址（2026-09-29）----------
#
# parse_pdf_list 會把**所有空白**清掉，才能把被 PDF 斷行的網址接回來。清單每頁
# 結尾是頁碼「19 / 37」，清掉空白後就直接黏在該頁最後一個網址尾巴：
#   ...VjC1l + 19 / 37  →  ...VjC1l19/37
# 黏在 query string 後面只是雜訊，但**黏在 Drive folder ID 尾巴上會把 ID 改壞**，
# 打 Drive API 必然 404——我們就把活的來源判成死的。實測台北市清單 440 案中
# 19 個 folder ID 被改壞，這 19 個資料夾其實全部讀得到，已累積 1,125 份 PDF。

from geobingan_sync.steps.sync_permits import strip_page_footer


def _parse_pages(ps, monkeypatch, texts, tmp_path):
    """多頁版：texts 是每頁的 extract_text() 內容。"""
    import geobingan_sync.steps.sync_permits as sp

    class _Reader:
        def __init__(self, f):
            self.pages = [_FakePage(t) for t in texts]
    monkeypatch.setattr(sp.pypdf, 'PdfReader', _Reader)
    f = tmp_path / 'l.pdf'
    f.write_bytes(b'%PDF-1.4')
    return ps.parse_pdf_list(str(f))


@pytest.mark.parametrize('text,page,total,expected', [
    ('...VjC1l\n19 / 37', 19, 37, '...VjC1l'),        # 實際 PDF 的樣子：頁碼前有換行
    ('...VjC1l \n 19 / 37 \n', 19, 37, '...VjC1l'),   # 前後多餘空白
    ('...VjC1l\n19/37', 19, 37, '...VjC1l'),          # 頁碼中間沒空白也算
    ('...X19\n19 / 37', 19, 37, '...X19'),            # 內容結尾剛好也是 19
    ('19 / 37', 19, 37, ''),                          # 整頁只有頁碼（開頭即邊界）
    ('...X\n1 / 37', 1, 37, '...X'),
])
def test_strip_page_footer_removes_real_footer(text, page, total, expected):
    assert strip_page_footer(text, page, total) == expected


@pytest.mark.parametrize('text,page,total', [
    # review P2 的反例：第 19 頁、合法網址正好以 /19/37 結尾，數字與頁碼完全相同。
    # 只比對數字分不出來，必須靠「頁碼前有邊界」才安全。
    ('https://nas.example/share/19/37', 19, 37),
    ('https://nas.example.com:5000/sharing/a/19/37', 19, 37),
    # 抽取結果把頁碼黏成一團（無任何邊界）→ 已無法與合法網址區分，保守保留。
    # 這種情況由 parse_pdf_list 的殘留掃描出聲，不會無聲吞掉。
    ('...VjC1l19 / 37', 19, 37),
    ('...VjC1l19/37', 19, 37),
    # 不是本頁頁碼 / 格式不同 → 一律不動
    ('https://x.test/a/12/30', 19, 37),
    ('...X第19頁共37頁', 19, 37),
])
def test_strip_page_footer_keeps_ambiguous_text(text, page, total):
    assert strip_page_footer(text, page, total) == text


def test_legit_url_ending_in_page_number_survives_real_footer(ps, monkeypatch, tmp_path):
    """第 19/37 頁上，網址結尾正好是 /19/37：只能剪掉真正的頁尾，網址要原封不動。"""
    url = 'https://nas.example.com:5000/sharing/a/19/37'
    pages = [f'第 {i} 頁內容\n{i} / 37' for i in range(1, 19)]
    pages.append(f'建照號碼 監造人 承造人 連結\n111建字第0099號 甲 乙 {url}\n19 / 37')
    pages += [f'第 {i} 頁內容\n{i} / 37' for i in range(20, 38)]
    out = _parse_pages(ps, monkeypatch, pages, tmp_path)
    assert out['111建字第0099號'] == url


def test_footer_without_boundary_is_kept_and_announced(ps, monkeypatch, tmp_path, capsys):
    """抽取沒給分隔時保守保留，但一定要出聲——保守不等於沉默。"""
    fid = '1AbCdEfGhIjKlMnOpQrStUvWxYz012345'
    pages = [f'110建字第0004號 甲 乙 https://drive.google.com/drive/folders/{fid}19 / 37']
    out = _parse_pages(ps, monkeypatch, pages, tmp_path)
    assert out['110建字第0004號'].endswith('19/37'), '無邊界時不剪'
    assert '殘留頁碼' in capsys.readouterr().out


def test_page_footer_does_not_corrupt_folder_id(ps, monkeypatch, tmp_path):
    """核心回歸：頁碼黏上去會讓 folder ID 多出幾位數，Drive 必回 404。"""
    fid = '1unymTUqmE9trxZQ2LJoN4qEBkoDVjC1l'
    pages = [
        f'建照號碼 監造人 承造人 連結\n111建字第1688號 甲 乙 '
        f'https://drive.google.com/drive/folders/{fid}\n1 / 2',
        '建照號碼 監造人 承造人 連結\n112建字第0022號 丙 丁 https://x.test/b 中文\n2 / 2',
    ]
    out = _parse_pages(ps, monkeypatch, pages, tmp_path)
    assert out['111建字第1688號'].endswith(fid), '頁碼不可黏進網址'
    assert ps.extract_folder_id_from_url(out['111建字第1688號']) == fid


def test_footer_on_query_string_also_stripped(ps, monkeypatch, tmp_path):
    url = 'https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz012345?usp=sharing'
    out = _parse_pages(ps, monkeypatch, [f'110建字第0001號 甲 乙 {url}\n1 / 1'], tmp_path)
    assert out['110建字第0001號'] == url


def test_url_legitimately_ending_in_digits_slash_digits_is_kept(ps, monkeypatch, tmp_path):
    """真實網址結尾也可能長得像頁碼，不能靠樣式亂剪。"""
    url = 'https://nas.example.com:5000/share/12/30'
    out = _parse_pages(ps, monkeypatch, [f'110建字第0002號 甲 乙 {url} 中文\n1 / 1'], tmp_path)
    assert out['110建字第0002號'] == url


def test_url_at_page_end_does_not_swallow_next_page_header(ps, monkeypatch, tmp_path):
    """去掉頁碼後，上一頁最後的網址會直接接到下一頁的表頭，中文要能截斷。"""
    fid = '1AbCdEfGhIjKlMnOpQrStUvWxYz012345'
    pages = [
        f'建照號碼 監造人 承造人 連結\n111建字第0100號 甲 乙 '
        f'https://drive.google.com/drive/folders/{fid}\n1 / 2',
        '建照號碼 監造人 承造人 連結\n111建字第0200號 丙 丁 https://b.test/two 中文\n2 / 2',
    ]
    out = _parse_pages(ps, monkeypatch, pages, tmp_path)
    assert ps.extract_folder_id_from_url(out['111建字第0100號']) == fid
    assert out['111建字第0200號'] == 'https://b.test/two'


def test_leaked_page_number_is_announced(ps, monkeypatch, tmp_path, capsys):
    """頁碼若不在頁尾（抽取順序改變），strip 會靜靜失效——至少要出聲。

    strip_page_footer 只剪**結尾**的頁碼。萬一 pypdf 哪天把頁碼排在中間，
    它就完全無效而且沒有任何徵兆，網址又開始被汙染。這條掃殘留特徵的警告
    是那時唯一的訊號。
    """
    fid = '1AbCdEfGhIjKlMnOpQrStUvWxYz012345'
    pages = [f'110建字第0003號 甲 乙 https://drive.google.com/drive/folders/{fid}\n'
             f'19 / 37\n建照號碼 監造人 承造人 連結']
    out = _parse_pages(ps, monkeypatch, pages, tmp_path)
    assert out['110建字第0003號'].endswith('19/37'), '前提：此情境確實會汙染網址'
    assert '殘留頁碼' in capsys.readouterr().out
