"""「這個檔案要不要鏡像、算不算可上傳的 PDF」——只有這一份定義。

#116 review P1：允收（`should_mirror`）、分類（`source_scan` 的 `pdf`／`other`）、
上傳（`select_pdfs_to_upload`）三處各寫了一套判定，於是兩個**真實存在**的情境會
得到互相矛盾的結論：

- `報告.pdf` + `application/octet-stream`（上傳端把型別標成通用值是常態）：
  會被鏡像，卻被歸類成 `other`，而 Drive 的 PDF 查詢也掃不到它
  → **一份真的報告永遠不會上傳，而且帳上看不出來。**
- `監測資料.jpg` + `application/pdf`（型別標錯）：會被算進 `source_scan.pdf`
  （看起來這案有報告在收），但上傳端的本機判定會拒絕
  → 統計與實際上傳語意不一致。

收斂成一支判定：**副檔名優先、mimeType 次之。** 副檔名是人為命名的意圖，
mimeType 常常是上傳端隨手給的；兩者衝突時相信副檔名。

這支刻意不 import 任何 Drive／requests 的東西：三個呼叫端（sync_permits、
upload_pdfs、match_permits）都要用它，放在有重依賴的模組裡會變成誰 import 誰的問題。
"""
from typing import Dict, Optional

PDF_MIME = 'application/pdf'

#: 副檔名 → 正規 mimeType。這張表同時決定「收不收」與「算什麼型別」，
#: 兩件事共用一份資料才不會漂開。
EXT_TO_MIME: Dict[str, str] = {
    'pdf': PDF_MIME,
    'xls': 'application/vnd.ms-excel',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'png': 'image/png',
    'heic': 'image/heic',
    'tif': 'image/tiff',
    'tiff': 'image/tiff',
    'webp': 'image/webp',
}

#: 沒有可辨識副檔名時，才用 mimeType 判。實測來源有
#: `application/vnd.ms-excel`（13 份 `.xls`）與 `image/jpeg`／`image/png`。
MIRROR_MIME_TYPES = frozenset({
    PDF_MIME,
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
})
MIRROR_MIME_PREFIXES = ('image/',)


def file_ext(name: str) -> str:
    """小寫副檔名（不含點）。沒有副檔名回空字串。"""
    name = name or ''
    return name.rsplit('.', 1)[-1].lower() if '.' in name else ''


def _recognised_mime(mime_type: str) -> Optional[str]:
    """mimeType 認得的話回它本身，否則 None。

    `application/octet-stream` 這類通用值**不算認得**——它只代表上傳端沒說，
    不代表內容是什麼，所以不能拿它去否決副檔名。
    """
    mime = (mime_type or '').lower()
    if mime in MIRROR_MIME_TYPES or mime.startswith(MIRROR_MIME_PREFIXES):
        return mime
    return None


def canonical_mime(name: str, mime_type: str = '') -> Optional[str]:
    """這個檔案在我們的鏡像裡應該算什麼型別。`None` ＝ 不鏡像。

    副檔名認得就以它為準，副檔名不認得才看 mimeType（Drive 的掃描結果有時
    除了檔名什麼都沒帶）。
    """
    return EXT_TO_MIME.get(file_ext(name)) or _recognised_mime(mime_type)


def has_type_conflict(name: str, mime_type: str = '') -> bool:
    """副檔名與 mimeType **都認得**、卻互相矛盾。

    例：`監測資料.jpg` + `application/pdf`，或 `報告.pdf` + `image/jpeg`。
    這種檔案的真實內容只有讀位元組才知道，光看中介資料無從判斷。
    注意 `報告.pdf` + `application/octet-stream` **不算**矛盾——通用型別只代表
    上傳端沒說，那是常態，不可因此否決副檔名。
    """
    ext_mime = EXT_TO_MIME.get(file_ext(name))
    src_mime = _recognised_mime(mime_type)
    return bool(ext_mime and src_mime and ext_mime != src_mime)


def should_mirror(name: str, mime_type: str = '') -> bool:
    """要不要鏡像進共享雲端硬碟。矛盾的也收——鏡像是免費的。"""
    return canonical_mime(name, mime_type) is not None


def is_uploadable_pdf(name: str, mime_type: str = '') -> bool:
    """這一份會被送去 riskmap 解析嗎。

    `source_scan` 的 `pdf` 欄位、`permit_registry` 的 `pdf_count`、上傳候選的
    篩選，三者都要以這一支為準——不然「有幾份報告在收」和「實際上傳了幾份」
    會是兩個不同的數字，而且沒人知道差在哪（#116 review P1）。

    **型別矛盾時一律不送。** 兩邊都認得卻不一致，代表我們不知道這是什麼；
    代價是不對稱的：誤送一張圖是 US$0.3 的無效支出加一次失敗解析，漏送一份
    報告則會在 `source_scan` 的 `other` 裡看得見（它仍然被鏡像了）。
    所以往「不送」那一側倒，而且留下看得見的痕跡。
    """
    return canonical_mime(name, mime_type) == PDF_MIME and \
        not has_type_conflict(name, mime_type)
