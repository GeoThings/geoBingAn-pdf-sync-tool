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


def test_a_jpeg_mislabelled_as_pdf_is_still_rejected():
    """副檔名是 .pdf 但 mimeType 說是圖片——兩個都看才擋得住。"""
    picked, counts = select_pdfs_to_upload(
        [_item('監測資料.pdf', 'image/jpeg')], uploaded_files=[], cutoff=CUTOFF)
    assert picked == []
    assert counts['not_pdf'] == 1


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
        assert "mimeType = 'application/pdf'" in q, (
            f'掃描查詢少了型別條件，非 PDF 會直接流進上傳候選：{q}')


def test_registry_scan_also_asks_for_pdfs_only():
    """`permit_registry` 的 `pdf_count` 是「有幾份報告在收」的依據。

    鏡像進來的 .xls 若被算進去，那個指標就變成假的——13 個月的 Excel 會讓
    110建字第0038號 看起來有 13 份報告。
    """
    import inspect
    from geobingan_sync.steps import match_permits

    src = inspect.getsource(match_permits)
    idx = src.index("permit_files = {}")
    window = src[idx:idx + 600]
    assert "mimeType='application/pdf'" in window, (
        'registry 掃描少了型別條件，pdf_count 會被鏡像的非 PDF 撐大')


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
