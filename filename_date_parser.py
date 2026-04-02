"""從 PDF 檔名解析日期的工具模組。

獨立於 config.py 和 Google Drive API，可安全用於測試。
"""
import re
from datetime import datetime
from typing import Optional

# 檔名日期過濾：2026 農曆新年（正月初一）= 2026-02-17
FILENAME_DATE_CUTOFF = datetime(2026, 2, 17)


def parse_date_from_filename(filename: str) -> Optional[datetime]:
    """從 PDF 檔名中解析日期。支援多種格式：
    - 民國年7碼: 1150311
    - 民國年點分隔: 115.03.24
    - 民國年中文: 115年03月09日
    - 民國年嵌入文字: 連雲玥恒1150331報告
    - 西元年連字號: 2026-02-23
    - 西元年8碼: 20260303
    - 短日期+路徑推斷: 0303觀測報告（從路徑取年份）
    回傳 datetime 或 None（無法解析時）
    """
    basename = filename.replace('.pdf', '').replace('.PDF', '')

    # 模式1: 西元年完整格式 2026-02-23 或 2026-03-01
    m = re.search(r'(20\d{2})-(\d{2})-(\d{2})', basename)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # 模式1b: 西元年緊湊格式 20260303
    m = re.search(r'(20\d{2})(\d{2})(\d{2})', basename)
    if m:
        try:
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return datetime(year, month, day)
        except ValueError:
            pass

    # 模式2: 民國年「115年03月09日」或「115年3月9日」
    m = re.search(r'(\d{2,3})年(\d{1,2})月(\d{1,2})日', basename)
    if m:
        try:
            roc_year = int(m.group(1))
            return datetime(roc_year + 1911, int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # 模式3: 民國年點分隔 115.03.24 或 115.3.24
    m = re.search(r'(\d{3})\.(\d{1,2})\.(\d{1,2})', basename)
    if m:
        try:
            roc_year = int(m.group(1))
            if 100 <= roc_year <= 120:
                return datetime(roc_year + 1911, int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # 模式4: 民國年7碼 1150311 (位於檔名開頭或底線/連字號後)
    m = re.search(r'(?:^|[_\-\s])(\d{3})(\d{2})(\d{2})(?:[_\-\s.]|$)', basename)
    if m:
        try:
            roc_year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            if 100 <= roc_year <= 120 and 1 <= month <= 12 and 1 <= day <= 31:
                return datetime(roc_year + 1911, month, day)
        except ValueError:
            pass

    # 模式5: 民國年7碼（嵌在文字中，如「連雲玥恒1150331報告」）
    m = re.search(r'(\d{7})', basename)
    if m:
        try:
            digits = m.group(1)
            roc_year = int(digits[:3])
            month = int(digits[3:5])
            day = int(digits[5:7])
            if 100 <= roc_year <= 120 and 1 <= month <= 12 and 1 <= day <= 31:
                return datetime(roc_year + 1911, month, day)
        except ValueError:
            pass

    # 模式6: 4碼日期 + 觀測報告（如「0303觀測報告」），從路徑推斷年份
    m = re.search(r'(\d{2})(\d{2})觀測報告', basename)
    if m:
        try:
            month = int(m.group(1))
            day = int(m.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                # 從路徑中找西元年
                year_match = re.search(r'(20\d{2})', basename)
                year = int(year_match.group(1)) if year_match else 2026
                return datetime(year, month, day)
        except ValueError:
            pass

    return None
