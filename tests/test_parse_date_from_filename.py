"""Tests for parse_date_from_filename().

Imports from filename_date_parser (standalone module) so tests run
without config.py, credentials, or any Google API dependencies.
"""
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from filename_date_parser import parse_date_from_filename, FILENAME_DATE_CUTOFF


class TestROCYear7Digits:
    """模式4/5: 民國年7碼 (1150311)"""

    def test_underscore_delimited(self):
        assert parse_date_from_filename("1150311_凌霄好室_安全監測系統報告-0079.pdf") == datetime(2026, 3, 11)

    def test_embedded_in_text(self):
        assert parse_date_from_filename("連雲玥恒1150331報告.pdf") == datetime(2026, 3, 31)

    def test_before_cny(self):
        assert parse_date_from_filename("1150105_凌霄好室_安全監測系統報告-0055.pdf") == datetime(2026, 1, 5)

    def test_roc_113(self):
        assert parse_date_from_filename("城甲士林1131028.pdf") == datetime(2024, 10, 28)

    def test_with_dash_prefix(self):
        assert parse_date_from_filename("璞承-璞開石1150209.pdf") == datetime(2026, 2, 9)


class TestROCYearDotSeparated:
    """模式3: 民國年點分隔 (115.03.24)"""

    def test_basic(self):
        assert parse_date_from_filename("115.03.24觀測報告.pdf") == datetime(2026, 3, 24)

    def test_two_digit_month_no_prefix(self):
        # 115.02 without day should not match (only 2 segments)
        result = parse_date_from_filename("(嘉鎷)115.02監測月報.pdf")
        assert result is None


class TestROCYearChinese:
    """模式2: 民國年中文 (115年03月09日)"""

    def test_full_format(self):
        assert parse_date_from_filename("士林區蘭雅段新建案工地監測報告_115年03月09日.pdf") == datetime(2026, 3, 9)

    def test_with_prefix(self):
        assert parse_date_from_filename("國產建材實業A棟企業總部大樓-115年02月24日量測報告.pdf") == datetime(2026, 2, 24)


class TestWesternYearHyphenated:
    """模式1: 西元年連字號 (2026-02-23)"""

    def test_basic(self):
        assert parse_date_from_filename("專案區間報告書-2026-02-23 00_00.pdf") == datetime(2026, 2, 23)

    def test_in_parentheses(self):
        assert parse_date_from_filename("(基地)報告書-2026-03-01 00_00-2026-03-08.pdf") == datetime(2026, 3, 1)


class TestWesternYear8Digits:
    """模式1b: 西元年緊湊格式 (20260303)"""

    def test_basic(self):
        assert parse_date_from_filename("report_20260303.pdf") == datetime(2026, 3, 3)

    def test_embedded(self):
        assert parse_date_from_filename("監測報告20260215結果.pdf") == datetime(2026, 2, 15)


class TestShortDateWithContext:
    """模式6: 短日期+觀測報告 (0303觀測報告)"""

    def test_with_year_in_path(self):
        assert parse_date_from_filename("建字123號/2026/全球人壽新總部大樓0303觀測報告.pdf") == datetime(2026, 3, 3)

    def test_without_year_defaults_2026(self):
        assert parse_date_from_filename("全球人壽新總部大樓0303觀測報告.pdf") == datetime(2026, 3, 3)


class TestUnparseable:
    """無法解析日期的檔名"""

    def test_no_date(self):
        assert parse_date_from_filename("監測應變計畫.pdf") is None

    def test_month_only(self):
        assert parse_date_from_filename("(嘉鎷)115.02監測月報.pdf") is None


class TestCutoffBoundary:
    """農曆新年邊界測試 (cutoff = 2026-02-17)"""

    def test_cutoff_date_excluded(self):
        """初一當天不應納入（> 而非 >=）"""
        d = parse_date_from_filename("1150217_report.pdf")
        assert d == datetime(2026, 2, 17)
        assert not (d > FILENAME_DATE_CUTOFF)  # 當天不通過

    def test_day_after_cutoff_included(self):
        """初二應納入"""
        d = parse_date_from_filename("1150218_report.pdf")
        assert d == datetime(2026, 2, 18)
        assert d > FILENAME_DATE_CUTOFF

    def test_day_before_cutoff_excluded(self):
        """除夕不應納入"""
        d = parse_date_from_filename("1150216_report.pdf")
        assert d == datetime(2026, 2, 16)
        assert not (d > FILENAME_DATE_CUTOFF)
