"""鏡像進雲端硬碟的非 PDF **絕不可**上傳到 riskmap。

2026-10-12 Zhe 定的範圍：收圖片和 .xls，但不上傳。這個約束的代價是不對稱的——
漏收一份報告是看得見的缺口，誤送一張 jpg 進解析佇列則是 US$0.3 的無效支出、
而且會把當日 66 份的額度排擠掉真正的報告（PROD-233／348 的日上限）。

防線有兩道，而且刻意互相獨立：
1. 伺服器端：掃描查詢帶 `mimeType='application/pdf'`（批次、逐資料夾回退，
   以及 match_permits 的 registry 掃描）。
2. 本機純函式：`select_pdfs_to_upload` 自己再剔除非 PDF。

第 2 道的存在理由就是第 1 道是單點：查詢字串改一個字、或哪天換別的列檔方式，
非 PDF 就直接流進佇列。這一檔同時盯住兩道。
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geobingan_sync.steps import upload_pdfs as up
from geobingan_sync.steps.upload_pdfs import select_pdfs_to_upload

CUTOFF = datetime(2026, 9, 1)


def _item(name, mime=None, folder='110建字第0038號'):
    it = {'id': 'x' + name, 'name': name, 'folder_name': folder,
          'modifiedTime': '2026-10-01T02:00:37.030Z'}
    if mime is not None:
        it['mimeType'] = mime
    return it


# ───────────────── 第 2 道：純函式自己剔除 ─────────────────

def test_mirrored_non_pdfs_are_not_selected_for_upload():
    """真正的碰撞：把實測來源那幾個檔名混進清單，只有 PDF 能被選中。"""
    picked, counts = select_pdfs_to_upload([
        _item('北投觀測資料114年11月.xls', 'application/vnd.ms-excel'),
        _item('富邦人壽承德大樓北基地_監測資料1131223.jpg', 'image/jpeg'),
        _item('QR-114建字第0607號-安全觀測報告.jpg', 'image/jpeg'),
        _item('3E6E9YEBEO.png', 'image/png'),
        _item('112建0079-初值報告20260930.pdf', 'application/pdf'),
    ], uploaded_files=[], cutoff=CUTOFF)

    assert [p['name'] for p in picked] == ['112建0079-初值報告20260930.pdf']
    assert counts['not_pdf'] == 4, '剔除要記在帳上，不可無聲丟掉'


def test_scan_results_without_mimetype_fall_back_to_the_extension():
    """回退掃描路徑的 fields 不含 mimeType，只看它就會整批放行。"""
    picked, counts = select_pdfs_to_upload([
        _item('北投觀測資料114年11月.xls'),
        _item('112建0079-初值報告20260930.pdf'),
    ], uploaded_files=[], cutoff=CUTOFF)
    assert [p['name'] for p in picked] == ['112建0079-初值報告20260930.pdf']
    assert counts['not_pdf'] == 1


@pytest.mark.parametrize('name,mime', [
    # 兩個方向的型別矛盾：中介資料互相打架時，我們其實不知道這是什麼
    ('監測資料20260930.pdf', 'image/jpeg'),
    ('監測資料20260930.jpg', 'application/pdf'),
])
def test_conflicting_type_metadata_is_never_uploaded(name, mime):
    """副檔名與 mimeType 都認得卻矛盾 → 不送解析（但仍會被鏡像）。

    ⚠️ 這條的第一版用了沒有日期的檔名 `監測資料.pdf`，於是它是被 `no_date`
    擋掉的，不是被型別判定擋掉的——**測試過了，但過的理由是錯的**。檔名一定要
    帶得出日期，才真的碰到要驗的那條判斷。
    """
    picked, counts = select_pdfs_to_upload(
        [_item(name, mime)], uploaded_files=[], cutoff=CUTOFF)
    assert counts['no_date'] == 0, '檔名要解析得出日期，否則驗到的是別條規則'
    assert picked == []
    assert counts['not_pdf'] == 1


def test_a_generic_mimetype_does_not_block_a_real_pdf():
    """`報告.pdf` + `application/octet-stream` 是**真的報告**，必須上傳。

    這就是 #116 review P1 的第一個情境：通用型別只代表上傳端沒說。原本鏡像端
    收了、分類端算成 other、上傳端又拒絕，於是一份真的報告永遠不會上傳。
    """
    picked, counts = select_pdfs_to_upload(
        [_item('112建0079-初值報告20260930.pdf', 'application/octet-stream')],
        uploaded_files=[], cutoff=CUTOFF)
    assert [p['name'] for p in picked] == ['112建0079-初值報告20260930.pdf']
    assert counts['not_pdf'] == 0


def test_real_pdfs_are_still_selected():
    """反面對照：這道防線不可把真的 PDF 擋掉，否則上面三條無論怎麼寫都會過。"""
    picked, _ = select_pdfs_to_upload(
        [_item('112建0079-初值報告20260930.pdf', 'application/pdf')],
        uploaded_files=[], cutoff=CUTOFF)
    assert len(picked) == 1


# ───────────────── 第 1 道：掃描查詢仍以 mimeType 為條件 ─────────────────

class _FakeFilesApi:
    def __init__(self, script):
        self.script, self.queries = list(script), []

    def list(self, **kwargs):
        self.queries.append(kwargs.get('q', ''))
        item = self.script.pop(0) if self.script else {'files': []}

        class _Req:
            def execute(_self):
                # 腳本裡的例外要**拋**而不是回傳。第一版寫成回傳，production
                # 於是拿到一個 HttpError 物件去 .get()，報的是 AttributeError，
                # 看起來像 production 壞了而不是測試寫錯。
                if isinstance(item, Exception):
                    raise item
                return item
        return _Req()


class _FakeService:
    def __init__(self, script):
        self._files = _FakeFilesApi(script)

    def files(self):
        return self._files


class _FakeResp:
    def __init__(self, status=403, reason='denied'):
        self.status, self.reason = status, reason


@pytest.mark.parametrize('force_fallback', [False, True])
def test_shared_drive_scan_asks_the_server_for_pdfs_only(force_fallback):
    """批次掃描**與**逐資料夾回退都必須把型別條件寫進查詢。

    行為斷言而不是讀原始碼：假的 service 把每次查詢字串記下來，少了條件就被抓到。
    兩條路要分別跑到——第一版只跑了批次（它成功了，回退根本沒被叫），
    docstring 卻寫著兩條都驗，那是假綠。403 不會被 paginate_files_list 重試，
    所以能穩定逼出回退路徑。
    """
    from googleapiclient.errors import HttpError
    from geobingan_sync.steps.upload_pdfs import list_all_pdfs_with_folder_info

    pdf_page = {'files': [{'id': 'p1', 'name': 'a.pdf', 'parents': ['f1'],
                           'modifiedTime': 'x'}]}
    script = ([HttpError(_FakeResp(), b'batch boom'), pdf_page]
              if force_fallback else [pdf_page])
    svc = _FakeService(script)

    result = list_all_pdfs_with_folder_info(svc, [{'id': 'f1', 'name': '110建字第0038號'}])

    assert [p['name'] for p in result] == ['a.pdf']
    assert len(svc._files.queries) == (2 if force_fallback else 1), (
        '回退路徑沒被走到的話，這一輪參數等於沒驗到')
    for q in svc._files.queries:
        # 兩個條件都要在。只斷言 mimeType 那一半的話，把查詢退回「只看
        # mimeType」時測試照樣會綠——而那正是 #116 review P1 的第一個情境：
        # 被標成 application/octet-stream 的真 PDF 永遠不會被發現。
        assert "mimeType = 'application/pdf'" in q, (
            f'掃描查詢少了型別條件：{q}')
        assert "name contains '.pdf'" in q, (
            f'掃描查詢少了副檔名條件，被標成通用型別的真 PDF 會掃不到：{q}')


def test_registry_pdf_count_uses_the_shared_verdict(monkeypatch):
    """`permit_registry` 的 `pdf_count` 是「有幾份報告在收」的依據。

    行為斷言而不是掃原始碼：第一版只檢查原始碼裡有沒有那串查詢，於是把本機的
    共用判定過濾拿掉時測試照樣綠（故障注入第 7 發）。這裡餵一份混合清單進去，
    直接看 `pdf_count` 數到幾。
    """
    from geobingan_sync.steps import match_permits as mp

    folder = {'id': 'f1', 'name': '110建字第0038號'}
    pages = [{'files': [
        # 真報告：型別被標成通用值，仍然要算
        {'name': '112建0079-初值報告20260930.pdf',
         'mimeType': 'application/octet-stream', 'parents': ['f1']},
        # 型別標成 PDF 的圖片：不可算進去（它會被查詢撈回來）
        {'name': '監測資料20260930.jpg', 'mimeType': 'application/pdf',
         'parents': ['f1']},
        # 鏡像進來的 Excel：不可算進去
        {'name': '北投觀測資料114年11月.xls',
         'mimeType': 'application/vnd.ms-excel', 'parents': ['f1']},
    ]}]
    queries = []

    class _Files:
        def list(self, **kw):
            queries.append(kw.get('q', ''))
            page = pages.pop(0) if pages else {'files': []}

            class _R:
                def execute(_s):
                    return page
            return _R()

    class _Svc:
        def files(self):
            return _Files()

    # 這三支是在 fetch_drive_pdf_names **函式內**才 import 的，所以要換
    # drive_utils 模組上的名字；換 match_permits 的同名屬性是空操作
    # （這個 session 裡已經在 find_adapter 上踩過同一個坑）。
    from geobingan_sync import drive_utils
    monkeypatch.setattr(drive_utils, 'list_top_level_folders',
                        lambda svc, drv: [folder])
    monkeypatch.setattr(drive_utils, 'list_all_subfolders', lambda svc, drv: {})
    monkeypatch.setattr(drive_utils, 'build_folder_resolver',
                        lambda folders, subs: (lambda fid: None))

    names, complete = mp.fetch_drive_pdf_names(_Svc())

    assert complete, '翻頁沒有中斷，應該回報掃描完整'
    assert names['110建字第0038號']['pdf_count'] == 1, (
        f"pdf_count 要只算真 PDF（1 份），實際 {names.get('110建字第0038號')}")
    assert any("name contains '.pdf'" in q for q in queries), (
        '查詢沒放寬，被標成通用型別的真 PDF 會從 registry 消失')


# ───────── 第 3 條路：copy_file 的下載回退不可把型別寫死 ─────────
#
# 故障注入時這條漏了：把回退路徑的 mimetype 寫死成 application/pdf，1076 條測試
# 全綠。但那條路是**真的會走到**的（files().copy 失敗時），而且後果剛好打穿上面
# 兩道防線——一張 jpg 以 mimeType=application/pdf 落進共享雲端硬碟，掃描查詢
# 就會認得它，於是它真的被送去解析。防線擋的是「非 PDF」，這裡是「被標成 PDF
# 的非 PDF」，第一道防線對它完全無效。

class _CopyFailsFilesApi:
    """files().copy 失敗 → 走下載＋重建；把 create 的參數記下來。"""

    def __init__(self):
        self.created = []

    def copy(self, **kwargs):
        from googleapiclient.errors import HttpError

        class _Req:
            def execute(_self):
                raise HttpError(_FakeResp(403, 'cannotCopyFile'), b'cannotCopyFile')
        return _Req()

    def get_media(self, **kwargs):
        return object()

    def create(self, **kwargs):
        self.created.append(kwargs)

        class _Req:
            def execute(_self):
                return {'id': 'new-id'}
        return _Req()


class _CopyFailsService:
    def __init__(self):
        self._files = _CopyFailsFilesApi()

    def files(self):
        return self._files


def test_copy_fallback_keeps_the_source_mime_type(monkeypatch):
    """下載回退重建檔案時，型別要跟著來源走。"""
    from geobingan_sync.steps import sync_permits as sp

    uploads = []

    class _FakeDownloader:
        def __init__(self, buf, request):
            self._buf = buf

        def next_chunk(self):
            self._buf.write(b'\xff\xd8\xff fake jpeg bytes')
            return None, True

    def _fake_upload(buf, mimetype=None, resumable=None):
        uploads.append(mimetype)
        return 'media'

    monkeypatch.setattr(sp, 'MediaIoBaseDownload', _FakeDownloader)
    monkeypatch.setattr(sp, 'MediaIoBaseUpload', _fake_upload)

    s = sp.PermitSync(city={'name': 'T', 'pdf_list_url': 'https://example.test/x.pdf'})
    svc = _CopyFailsService()
    monkeypatch.setattr(s, '_get_svc', lambda: svc)

    file_id, folder = s.copy_file('src', 'tgt', '監測資料1131223.jpg',
                                  mime_type='image/jpeg')

    assert file_id == 'new-id'
    assert uploads == ['image/jpeg'], (
        '上傳位元組時的型別寫死成 PDF，一張 jpg 就會以 application/pdf 落進雲端'
        '硬碟——掃描查詢認得它，於是它真的被送去 riskmap 解析')
    assert svc._files.created[0]['body']['mimeType'] == 'image/jpeg', (
        'create 的 body 型別也要跟著來源，這是 Drive 上最終被記錄的那個值')


def test_copy_fallback_still_defaults_to_pdf(monkeypatch):
    """反面對照：不傳 mime_type 的舊呼叫點（adapter 以外都是 PDF）行為不變。"""
    from geobingan_sync.steps import sync_permits as sp

    uploads = []
    monkeypatch.setattr(sp, 'MediaIoBaseDownload',
                        lambda buf, request: type('D', (), {
                            'next_chunk': lambda _s: (None, True)})())
    monkeypatch.setattr(sp, 'MediaIoBaseUpload',
                        lambda buf, mimetype=None, resumable=None:
                        uploads.append(mimetype) or 'media')

    s = sp.PermitSync(city={'name': 'T', 'pdf_list_url': 'https://example.test/x.pdf'})
    monkeypatch.setattr(s, '_get_svc', lambda: _CopyFailsService())
    s.copy_file('src', 'tgt', '報告.pdf')
    assert uploads == ['application/pdf']


# ───────── 三處必須給同一個答案（#116 review P1 的根因） ─────────

_CASES = [
    # (檔名, mimeType, 期望「會上傳 riskmap」)
    ('112建0079-初值報告20260930.pdf', 'application/pdf', True),
    ('112建0079-初值報告20260930.pdf', 'application/octet-stream', True),
    ('112建0079-初值報告20260930.pdf', '', True),
    ('北投觀測資料114年11月.xls', 'application/vnd.ms-excel', False),
    ('監測資料20260930.jpg', 'image/jpeg', False),
    ('監測資料20260930.jpg', 'application/pdf', False),      # 型別矛盾
    ('監測資料20260930.pdf', 'image/jpeg', False),           # 反向矛盾
    ('施工計畫書20260930.doc', 'application/msword', False),  # 連鏡像都不收
]


@pytest.mark.parametrize('name,mime,uploadable', _CASES)
def test_all_three_sites_agree(name, mime, uploadable, monkeypatch):
    """允收／分類／上傳三處對同一個檔案必須得到同一個結論。

    根因不是某一處寫錯，是三處各寫一套。所以這條測試直接比對三者：
    1. `mirror_types.is_uploadable_pdf`（共用判定）
    2. `sync_permit` 把它算進 `source_scan.pdf` 嗎（分類端）
    3. `select_pdfs_to_upload` 選中它嗎（上傳端）
    """
    from geobingan_sync.mirror_types import is_uploadable_pdf, should_mirror
    from geobingan_sync.steps import sync_permits as sp

    # ① 共用判定
    assert is_uploadable_pdf(name, mime) is uploadable

    # ② 分類端：有鏡像的才會進 source_scan，沒鏡像的只會進 skipped
    s = sp.PermitSync(city={'name': 'T', 'pdf_list_url': 'https://example.test/x.pdf'})
    svc_script = [{'files': [{'id': 'f1', 'name': name, 'mimeType': mime}]}]

    class _Files:
        def __init__(self, script):
            self.script = list(script)

        def list(self, **kw):
            item = self.script.pop(0) if self.script else {'files': []}

            class _R:
                def execute(_s):
                    return item
            return _R()

    class _Svc:
        def __init__(self, script):
            self._f = _Files(script)

        def files(self):
            return self._f

    monkeypatch.setattr(s, '_get_svc', lambda: _Svc(svc_script))
    monkeypatch.setattr(s, 'preload_target_files', lambda *a, **k: None)
    monkeypatch.setattr(s, 'check_file_exists', lambda *a, **k: True)
    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')
    entry = (s.state.get('source_scan') or {}).get('A建字第0001號')
    assert entry['pdf'] == (1 if uploadable else 0), (
        f'分類端與共用判定不一致：{entry}')
    if should_mirror(name, mime):
        assert entry['other'] == (0 if uploadable else 1)
        assert entry['skipped'] == 0
    else:
        assert entry['other'] == 0 and entry['skipped'] == 1

    # ③ 上傳端
    picked, _ = select_pdfs_to_upload([_item(name, mime)],
                                      uploaded_files=[], cutoff=CUTOFF)
    assert bool(picked) is uploadable, '上傳端與共用判定不一致'


# ───────── 鏡像落檔一律保留**來源型別** ─────────
#
# 這一節前一版寫的是「存成副檔名推出來的型別」，而那是錯的（#116 review 第二輪
# 指出）。正規化會把「副檔名與 mimeType 矛盾」從資料裡抹掉，而下一輪重新掃描時
# 我們只看得到鏡像裡的值：`監測資料.pdf` + `image/jpeg` 一旦被寫成
# application/pdf，下一輪就會被當成正常 PDF 送去解析——來源分類時擋住了，繞過
# 鏡像又回來了。
#
# 保留來源值則兩個方向都成立，因為判定每次都從 (檔名, mimeType) 重新推導：
#   .pdf + image/jpeg        → 矛盾 → 不上傳
#   .jpg + application/pdf   → 矛盾 → 不上傳
#   .pdf + octet-stream      → 通用型別不算認得 → 可上傳
# 前提是掃描的 fields 真的有取 mimeType（見 test_scan_fetches_mimetype）。

@pytest.mark.parametrize('name,source_mime', [
    ('監測資料20260930.jpg', 'application/pdf'),
    ('監測資料20260930.pdf', 'image/jpeg'),
    ('112建0079-初值報告20260930.pdf', 'application/octet-stream'),
    ('北投觀測資料114年11月.xls', 'application/octet-stream'),
])
def test_mirror_preserves_the_source_type(name, source_mime, monkeypatch):
    """鏡像落檔的型別＝來源原值，不正規化。

    `files().copy` 的主路徑本來就保留來源值（Drive 的行為），所以下載回退路徑
    也必須一致——同一個檔案不可因為走哪條路而被標成不同型別。
    """
    expected_stored = source_mime
    from geobingan_sync.steps import sync_permits as sp

    class _Files:
        def __init__(self, script):
            self.script = list(script)

        def list(self, **kw):
            item = self.script.pop(0) if self.script else {'files': []}

            class _R:
                def execute(_s):
                    return item
            return _R()

    class _Svc:
        def __init__(self, script):
            self._f = _Files(script)

        def files(self):
            return self._f

    s = sp.PermitSync(city={'name': 'T', 'pdf_list_url': 'https://example.test/x.pdf'})
    monkeypatch.setattr(s, '_get_svc',
                        lambda: _Svc([{'files': [{'id': 'f1', 'name': name,
                                                  'mimeType': source_mime}]}]))
    monkeypatch.setattr(s, 'preload_target_files', lambda *a, **k: None)
    monkeypatch.setattr(s, 'check_file_exists', lambda *a, **k: False)
    seen = []
    monkeypatch.setattr(s, 'copy_file',
                        lambda fid, tgt, fname, path='', mime_type=None:
                        (seen.append(mime_type) or 'new', tgt))

    s.sync_permit('A建字第0001號', 'https://drive.google.com/drive/folders/FID', 'tgt')

    assert seen == [expected_stored], (
        f'{name}（來源標 {source_mime}）在鏡像裡被存成 {seen}，'
        f'應該是 {expected_stored}')


def test_scan_fetches_mimetype_so_conflicts_survive(monkeypatch):
    """兩個掃描的 fields 都要取 mimeType。

    不取的話，鏡像裡那個 `監測資料.pdf`（實際是 image/jpeg）在掃描結果裡只有
    檔名，本機判定看不到矛盾 → 當成正常 PDF 送去解析。這就是 #116 review
    第二輪指出的「衝突訊號沒有跨鏡像保存」的後半段。
    """
    from geobingan_sync.steps.upload_pdfs import list_all_pdfs_with_folder_info
    from googleapiclient.errors import HttpError

    for force_fallback in (False, True):
        page = {'files': [{'id': 'p1', 'name': 'a.pdf', 'parents': ['f1'],
                           'mimeType': 'application/pdf', 'modifiedTime': 'x'}]}
        script = ([HttpError(_FakeResp(), b'boom'), page] if force_fallback else [page])
        svc = _FakeService(script)
        svc._files.fields = []
        orig_list = svc._files.list

        def _list(**kw):
            svc._files.fields.append(kw.get('fields', ''))
            return orig_list(**kw)

        svc._files.list = _list
        list_all_pdfs_with_folder_info(svc, [{'id': 'f1', 'name': '110建字第0038號'}])
        for f in svc._files.fields:
            assert 'mimeType' in f, (
                f'掃描沒取 mimeType（fallback={force_fallback}），'
                f'矛盾訊號在這一步就消失：{f}')


# ───────── 整條正式路徑：來源 → 鏡像落檔 → 重新掃描 → 上傳選擇 ─────────
#
# #116 review 第二輪的核心：前面每一段都各自有測試，但沒有一條把四段串起來，
# 所以「來源擋住了、繞過鏡像又回來」這種漏法 1092 條測試都看不到。

def test_source_to_mirror_to_scan_to_upload(monkeypatch):
    """四種型別組合走完整條路，只有真 PDF 可以到上傳候選。"""
    from geobingan_sync.steps import sync_permits as sp

    source_files = [
        # (檔名, 來源 mimeType, 期望「最後會上傳」)
        ('112建0079-初值報告20260930.pdf', 'application/pdf', True),
        ('112建0080-初值報告20260930.pdf', 'application/octet-stream', True),
        ('監測資料20260930.pdf', 'image/jpeg', False),        # 矛盾
        ('監測資料20260930.jpg', 'application/pdf', False),    # 反向矛盾
        ('北投觀測資料114年11月.xls', 'application/vnd.ms-excel', False),
    ]

    class _Files:
        def __init__(self, script):
            self.script = list(script)

        def list(self, **kw):
            item = self.script.pop(0) if self.script else {'files': []}

            class _R:
                def execute(_s):
                    return item
            return _R()

    class _Svc:
        def __init__(self, script):
            self._f = _Files(script)

        def files(self):
            return self._f

    # ① 來源 → 鏡像：記下「真的被寫進鏡像的 (檔名, 型別)」
    mirror = []
    s = sp.PermitSync(city={'name': 'T', 'pdf_list_url': 'https://example.test/x.pdf'})
    monkeypatch.setattr(s, '_get_svc', lambda: _Svc([{'files': [
        {'id': f'id{i}', 'name': n, 'mimeType': m}
        for i, (n, m, _) in enumerate(source_files)]}]))
    monkeypatch.setattr(s, 'preload_target_files', lambda *a, **k: None)
    monkeypatch.setattr(s, 'check_file_exists', lambda *a, **k: False)
    monkeypatch.setattr(s, 'copy_file',
                        lambda fid, tgt, fname, path='', mime_type=None:
                        (mirror.append((fname, mime_type)) or 'new', tgt))
    s.sync_permit('110建字第0038號', 'https://drive.google.com/drive/folders/FID', 'tgt')

    assert len(mirror) == len(source_files), '五個檔案都應該被鏡像'
    assert dict(mirror)['監測資料20260930.pdf'] == 'image/jpeg', (
        '鏡像把矛盾正規化掉了，下一輪就會把它當成正常 PDF')

    # ② 鏡像 → 重新掃描：用鏡像裡真正的 (檔名, 型別) 當掃描結果
    scanned = [{'id': f'm{i}', 'name': n, 'mimeType': m, 'parents': ['f1'],
                'modifiedTime': '2026-10-01T02:00:37.030Z'}
               for i, (n, m) in enumerate(mirror)]

    # ③ 掃描 → 上傳選擇
    picked, counts = select_pdfs_to_upload(
        [dict(x, folder_name='110建字第0038號') for x in scanned],
        uploaded_files=[], cutoff=CUTOFF)

    expected = sorted(n for n, _, up in source_files if up)
    assert sorted(p['name'] for p in picked) == expected, (
        f'整條路徑的結果不對。counts={counts}')
    assert counts['not_pdf'] == 3

    # ④ 來源分類與最終上傳結果必須是同一個數字
    entry = (s.state.get('source_scan') or {}).get('110建字第0038號')
    assert entry['pdf'] == len(expected), (
        f"source_scan 說 {entry['pdf']} 份會上傳，實際 {len(expected)} 份：{entry}")
