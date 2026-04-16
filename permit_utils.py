"""
建照相關純工具函數

無外部服務依賴，可安全在測試中 import。
"""
import re


def extract_name_from_filename(filename: str) -> str:
    """從 PDF 檔名提取建案名稱（去除日期、副檔名、建照號碼、通用詞）"""
    name = filename
    name = re.sub(r'\.pdf$', '', name, flags=re.IGNORECASE)
    paren_match = re.match(r'^[（(]([^\u0000-\u007F]{2,})[）)]', name)
    if paren_match:
        candidate = paren_match.group(1)
        rest = name[paren_match.end():]
        if re.match(r'[\d\s._\-]*(監測|觀測|報告|月報|週報|日報)', rest):
            return candidate
    name = re.sub(r'\d{2,3}建字第\d{3,5}號', '', name)
    name = re.sub(r'建照字號', '', name)
    name = re.sub(r'^\d{7}[_\s]*', '', name)
    name = re.sub(r'\d{4}-\d{2}-\d{2}[^)]*', '', name)
    name = re.sub(r'\d{3}\.\d{2}\.\d{2}', '', name)
    name = re.sub(r'\d{2,3}年\d{1,2}月\d{1,2}日', '', name)
    name = re.sub(r'\d{4}觀測報告$', '', name)
    name = re.sub(r'1\d{6}', '', name)
    name = re.sub(r'\d{2}月\d{2}日', '', name)
    name = re.sub(r'[-_\s]*(觀測報告|觀測結果|監測報告|安全監測系統報告|量測報告|監測週報|監測月報|日報告|週報|月報|安全觀測報告書|安全觀測系統\d*月?報告書?).*$', '', name)
    name = re.sub(r'[-_\s]*連續壁?初值$', '', name)
    name = re.sub(r'[-_\s]*(上傳|公告|更正|報告書|報告|觀測報告)$', '', name)
    name = re.sub(r'[-_\s]*NO\.\d+$', '', name)
    name = re.sub(r'[-_\s]*\d+$', '', name)
    name = re.sub(r'^\d+[-_.\s]*', '', name)
    name = re.sub(r'^P-\s*', '', name)
    name = re.sub(r'[-_\s]*初始?值.*$', '', name)
    name = re.sub(r'[-_\s]*_compressed$', '', name)
    name = re.sub(r'[-_\s]*日報表$', '', name)
    name = name.strip(' -_()（）/\\')
    name = re.sub(r'^[()（）\s]+', '', name)
    name = re.sub(r'[（(][^）)]*$', '', name)
    name = re.sub(r'^[^（(]*[）)]', '', name)
    name = re.sub(r'^\(基地\)', '', name)
    name = re.sub(r'-專案區間報告書$', '', name)
    parts = name.split('_')
    if len(parts) >= 2 and parts[0] in parts[1]:
        name = parts[1]

    name = re.sub(r'\d{1,2}月\d{1,2}日$', '', name).strip(' -_')
    name = re.sub(r'\d{7,}', '', name).strip(' -_')

    _GENERIC_NAMES = {'觀測紀錄', '監測數據', '安全觀測系統', '初始值', '整體進度',
               '觀測儀器配置圖', '量測報表', '報表', '安全觀測', '專案區間報告書',
               '基地', '匝道', '捷運', '觀測月報', '觀測', '監測報表', '工地',
               '新建工程', '集合住宅', '住宅大樓', '商業大樓', '',
               '觀測圖示及觀測紀錄', '專案區間', '安全', '初值', '安全觀測系統報告書',
               '連續壁完整性試驗'}
    if re.match(r'^[\d月日年.]+$', name):
        return ''
    if re.match(r'^鍵字第', name):
        return ''
    if name in _GENERIC_NAMES or len(name) < 2:
        return ''
    if re.match(r'^\d{2,3}建字第', name):
        return ''
    if name.startswith('.') or '該網站' in name or '自行維護' in name:
        return ''
    if re.match(r'^安全觀測系統', name):
        return ''
    return name
