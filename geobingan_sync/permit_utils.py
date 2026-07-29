"""
建照相關純工具函數

無外部服務依賴，可安全在測試中 import。
"""
import re
from typing import Optional

# ==================== 預編譯 Regex ====================

_RE_PERMIT_NORMALIZE = re.compile(r'(\d{2,3})\s*建\s*字?\s*第?\s*0*(\d{3,5})\s*號?')

_RE_PDF_EXT = re.compile(r'\.pdf$', re.IGNORECASE)
_RE_PAREN_NAME = re.compile(r'^[（(]([^\u0000-\u007F]{2,})[）)]')
_RE_PAREN_REST = re.compile(r'[\d\s._\-]*(監測|觀測|報告|月報|週報|日報)')
_RE_PERMIT_NO = re.compile(r'\d{2,3}建字第\d{3,5}號')
_RE_LICENSE_LABEL = re.compile(r'建照字號')
_RE_ROC7_START = re.compile(r'^\d{7}[_\s]*')
_RE_WESTERN_DATE = re.compile(r'\d{4}-\d{2}-\d{2}[^)]*')
_RE_DOT_DATE = re.compile(r'\d{3}\.\d{2}\.\d{2}')
_RE_CN_DATE = re.compile(r'\d{2,3}年\d{1,2}月\d{1,2}日')
_RE_SHORT_OBS = re.compile(r'\d{4}觀測報告$')
_RE_ROC7_EMBED = re.compile(r'1\d{6}')
_RE_MMDD = re.compile(r'\d{2}月\d{2}日')
_RE_GENERIC_SUFFIX = re.compile(r'[-_\s]*(觀測報告|觀測結果|監測報告|安全監測系統報告|量測報告|監測週報|監測月報|日報告|週報|月報|安全觀測報告書|安全觀測系統\d*月?報告書?).*$')
_RE_WALL_INIT = re.compile(r'[-_\s]*連續壁?初值$')
_RE_UPLOAD_SUFFIX = re.compile(r'[-_\s]*(上傳|公告|更正|報告書|報告|觀測報告)$')
_RE_NO_SUFFIX = re.compile(r'[-_\s]*NO\.\d+$')
_RE_TRAILING_NUM = re.compile(r'[-_\s]*\d+$')
_RE_LEADING_NUM = re.compile(r'^\d+[-_.\s]*')
_RE_P_PREFIX = re.compile(r'^P-\s*')
_RE_INIT_VALUE = re.compile(r'[-_\s]*初始?值.*$')
_RE_COMPRESSED = re.compile(r'[-_\s]*_compressed$')
_RE_DAILY_REPORT = re.compile(r'[-_\s]*日報表$')
_RE_LEADING_PAREN = re.compile(r'^[()（）\s]+')
_RE_UNCLOSED_PAREN = re.compile(r'[（(][^）)]*$')
_RE_UNOPENED_PAREN = re.compile(r'^[^（(]*[）)]')
_RE_BASE_PREFIX = re.compile(r'^\(基地\)')
_RE_INTERVAL_SUFFIX = re.compile(r'-專案區間報告書$')
_RE_TRAILING_MMDD = re.compile(r'\d{1,2}月\d{1,2}日$')
_RE_LONG_DIGITS = re.compile(r'\d{7,}')
_RE_ONLY_DATE_CHARS = re.compile(r'^[\d月日年.]+$')
_RE_BAD_KEY = re.compile(r'^鍵字第')
_RE_PERMIT_START = re.compile(r'^\d{2,3}建字第')
_RE_SAFETY_OBS = re.compile(r'^安全觀測系統')

# ==================== 通用名稱過濾集 ====================

_GENERIC_NAMES = frozenset({
    '觀測紀錄', '監測數據', '安全觀測系統', '初始值', '整體進度',
    '觀測儀器配置圖', '量測報表', '報表', '安全觀測', '專案區間報告書',
    '基地', '匝道', '捷運', '觀測月報', '觀測', '監測報表', '工地',
    '新建工程', '集合住宅', '住宅大樓', '商業大樓', '',
    '觀測圖示及觀測紀錄', '專案區間', '安全', '初值', '安全觀測系統報告書',
    '連續壁完整性試驗',
})


# ==================== 公開函數 ====================

def normalize_permit(raw: str) -> Optional[str]:
    """標準化建照號碼格式（處理空格、缺字、zero-padding）"""
    m = _RE_PERMIT_NORMALIZE.search(raw)
    if m:
        year = m.group(1)
        num = m.group(2).zfill(4)
        return f'{year}建字第{num}號'
    return None


def extract_name_from_filename(filename: str) -> str:
    """從 PDF 檔名提取建案名稱（去除日期、副檔名、建照號碼、通用詞）"""
    name = filename
    name = _RE_PDF_EXT.sub('', name)
    paren_match = _RE_PAREN_NAME.match(name)
    if paren_match:
        candidate = paren_match.group(1)
        rest = name[paren_match.end():]
        if _RE_PAREN_REST.match(rest):
            return candidate
    name = _RE_PERMIT_NO.sub('', name)
    name = _RE_LICENSE_LABEL.sub('', name)
    name = _RE_ROC7_START.sub('', name)
    name = _RE_WESTERN_DATE.sub('', name)
    name = _RE_DOT_DATE.sub('', name)
    name = _RE_CN_DATE.sub('', name)
    name = _RE_SHORT_OBS.sub('', name)
    name = _RE_ROC7_EMBED.sub('', name)
    name = _RE_MMDD.sub('', name)
    name = _RE_GENERIC_SUFFIX.sub('', name)
    name = _RE_WALL_INIT.sub('', name)
    name = _RE_UPLOAD_SUFFIX.sub('', name)
    name = _RE_NO_SUFFIX.sub('', name)
    name = _RE_TRAILING_NUM.sub('', name)
    name = _RE_LEADING_NUM.sub('', name)
    name = _RE_P_PREFIX.sub('', name)
    name = _RE_INIT_VALUE.sub('', name)
    name = _RE_COMPRESSED.sub('', name)
    name = _RE_DAILY_REPORT.sub('', name)
    name = name.strip(' -_()（）/\\')
    name = _RE_LEADING_PAREN.sub('', name)
    name = _RE_UNCLOSED_PAREN.sub('', name)
    name = _RE_UNOPENED_PAREN.sub('', name)
    name = _RE_BASE_PREFIX.sub('', name)
    name = _RE_INTERVAL_SUFFIX.sub('', name)
    parts = name.split('_')
    if len(parts) >= 2 and parts[0] in parts[1]:
        name = parts[1]

    name = _RE_TRAILING_MMDD.sub('', name).strip(' -_')
    name = _RE_LONG_DIGITS.sub('', name).strip(' -_')

    if _RE_ONLY_DATE_CHARS.match(name):
        return ''
    if _RE_BAD_KEY.match(name):
        return ''
    if name in _GENERIC_NAMES or len(name) < 2:
        return ''
    if _RE_PERMIT_START.match(name):
        return ''
    if name.startswith('.') or '該網站' in name or '自行維護' in name:
        return ''
    if _RE_SAFETY_OBS.match(name):
        return ''
    return name
