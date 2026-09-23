"""從 PDF 檔名解析日期的工具模組。

獨立於 config.py 和 Google Drive API，可安全用於測試。
"""
import re
from datetime import datetime, timedelta
from typing import Optional


def _month_end(year: int, month: int) -> datetime:
    """月底最後一天。月度報告（YYYYMM、民國年月）回傳月底，對 cutoff 比較較寬容。"""
    if month == 12:
        return datetime(year, 12, 31)
    return datetime(year, month + 1, 1) - timedelta(days=1)

# 檔名日期過濾：2026 農曆新年（正月初一）= 2026-02-17
FILENAME_DATE_CUTOFF = datetime(2026, 2, 17)


# 解析結果的理智區間。監測報告的檔名日期**不可能是未來**，也不可能早於本制度存在之前。
# 超出區間視為「解析失敗」（回 None），與既有的 no_date 行為一致：不上傳、不計入新鮮度、
# 排序時排最後。比硬塞一個錯的日期安全——錯的未來日會讓報告被誤判為「最新」而插隊。
MIN_REASONABLE = datetime(2000, 1, 1)      # 民國 89 年；比樣式契約的下限（民國100）再寬，只擋明顯垃圾
FUTURE_TOLERANCE_DAYS = 2      # 容許時區與跨日誤差


def _sane(d: Optional[datetime], now: Optional[datetime] = None) -> Optional[datetime]:
    if d is None:
        return None
    now = now or datetime.now()
    if d < MIN_REASONABLE or d > now + timedelta(days=FUTURE_TOLERANCE_DAYS):
        return None
    return d


def parse_date_from_filename(filename: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """從 PDF 檔名中解析日期。支援多種格式：
    - 民國年7碼: 1150311
    - 民國年點分隔: 115.03.24
    - 民國年中文: 115年03月09日
    - 民國年嵌入文字: 連雲玥恒1150331報告
    - 西元年連字號: 2026-02-23
    - 西元年8碼: 20260303
    - 短日期+路徑推斷: 0303觀測報告（從路徑取年份）
    - 裸民國年前綴+短日期: 115裕光東湖觀測報告0721 / 114.11/璞昌1127觀測報告
      （排除 11X建字第… 建照號；範圍 100–130）
    - 民國年月/西元年月（無日）: 114年04月、監測月報202512 → 回傳月底
    回傳 datetime 或 None（無法解析時）
    """
    return _sane(_parse_raw(filename), now)


def _parse_raw(filename: str) -> Optional[datetime]:
    """純樣式比對，不做合理性檢查（由 _sane 負責）。"""
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

    # 模式1c: 西元年中文格式「2026年08月21日」。必須在民國年規則之前，
    # 否則 `(\d{2,3})年` 會吃掉 4 位年份的尾三碼（2026→「026」→民國 26 年→1937）。
    # 273 筆「忠孝勤靜…-監測報表-2026年08月21日(週報).pdf」曾被解析成 1930 年代。
    m = re.search(r'(20\d{2})年(\d{1,2})月(\d{1,2})日', basename)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # 模式2: 民國年「115年03月09日」或「115年3月9日」
    # (?<!\d) 防止吃到西元年尾碼（見模式 1c）
    m = re.search(r'(?<!\d)(\d{2,3})年(\d{1,2})月(\d{1,2})日', basename)
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

    # 模式6: 4碼日期 + 觀測報告（前或後都接受），從路徑取年份（西元年或民國年）
    # 例「0303觀測報告」、「觀測報告1208」
    m = re.search(r'(?:(\d{2})(\d{2})觀測報告|觀測報告(\d{2})(\d{2}))', basename)
    if m:
        try:
            month = int(m.group(1) or m.group(3))
            day = int(m.group(2) or m.group(4))
            if 1 <= month <= 12 and 1 <= day <= 31:
                # 先試西元年
                year_match = re.search(r'(20\d{2})', basename)
                if year_match:
                    return datetime(int(year_match.group(1)), month, day)
                # 再試民國年（folder 像 `114年`）
                roc_match = re.search(r'(?<!\d)(\d{2,3})年(?!\d)', basename)
                if roc_match:
                    roc_year = int(roc_match.group(1))
                    if 100 <= roc_year <= 130:
                        return datetime(roc_year + 1911, month, day)
                # 再試檔名開頭的裸民國年前綴（如「115裕光東湖觀測報告0721」）。
                # 排除「11X建字第…」——那是建照核發年份、不是報告年份，
                # 誤組會把報告標錯年污染後端監測歷史。
                prefix_match = re.search(r'^(1[0-2]\d|130)(?!\d)(?!建字)', basename)
                if prefix_match:
                    roc_year = int(prefix_match.group(1))
                    if 100 <= roc_year <= 130:
                        return datetime(roc_year + 1911, month, day)
        except ValueError:
            pass

    # 模式7: 民國年月（無日）「114年04月」、「113.12月」、「113年12月」
    # 月度報告，回傳該月最後一天（對 cutoff 寬容）
    m = re.search(r'(?<!\d)(\d{2,3})[年.](\d{1,2})月(?!\d)', basename)   # (?<!\d) 見模式 1c
    if m:
        try:
            roc_year = int(m.group(1))
            month = int(m.group(2))
            if 100 <= roc_year <= 130 and 1 <= month <= 12:
                return _month_end(roc_year + 1911, month)
        except ValueError:
            pass

    # 模式8: YYYYMM 6 碼月份（如「監測月報202512」）
    # 月度報告，回傳該月最後一天
    m = re.search(r'(?<!\d)(20\d{2})(\d{2})(?!\d)', basename)
    if m:
        try:
            year = int(m.group(1))
            month = int(m.group(2))
            if 1 <= month <= 12:
                return _month_end(year, month)
        except ValueError:
            pass

    return None
