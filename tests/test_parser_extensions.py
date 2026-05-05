"""Tests for parser extensions added in PR #38: 民國年月、YYYYMM、folder 民國年 + MMDD.

Covers patterns 6 (extended), 7, 8 added to filename_date_parser.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from filename_date_parser import _month_end, parse_date_from_filename


class TestMonthEnd:
    def test_january(self):
        assert _month_end(2026, 1) == datetime(2026, 1, 31)

    def test_february_non_leap(self):
        assert _month_end(2025, 2) == datetime(2025, 2, 28)

    def test_february_leap(self):
        assert _month_end(2024, 2) == datetime(2024, 2, 29)

    def test_april(self):
        assert _month_end(2026, 4) == datetime(2026, 4, 30)

    def test_december(self):
        assert _month_end(2026, 12) == datetime(2026, 12, 31)


class TestPattern6ExtendedFolderROC:
    """模式 6 擴充：folder 民國年 + filename MMDD（如 `114年/觀測報告1208`）"""

    def test_roc_folder_mmdd_after_keyword(self):
        # 民國 114 年 = 2025
        assert parse_date_from_filename(
            '114年/金藏奧斯卡觀測報告1208(中興).pdf'
        ) == datetime(2025, 12, 8)

    def test_western_folder_still_works(self):
        # Regression: 西元年 folder 仍然 OK
        assert parse_date_from_filename(
            '2025/全球人壽新總部大樓0624觀測報告.pdf'
        ) == datetime(2025, 6, 24)

    def test_no_year_in_path_returns_none(self):
        # folder 沒年份線索就不要硬猜
        assert parse_date_from_filename(
            '111建字第0249號/信義雙星二期1009觀測報告.pdf'
        ) is None


class TestPattern7ROCYearMonth:
    """模式 7：民國年月（無日），如 `114年04月`、`113.12月`"""

    def test_roc_year_month_with_zai(self):
        # 民國 114 年 04 月 = 2025-04, 月底 2025-04-30
        assert parse_date_from_filename(
            '安全觀測系統114年04月報告書.pdf'
        ) == datetime(2025, 4, 30)

    def test_roc_year_month_dot_separator(self):
        # 民國 113.12 月 = 2024-12, 月底 2024-12-31
        assert parse_date_from_filename(
            '建國工程-月報告(113.12月).pdf'
        ) == datetime(2024, 12, 31)

    def test_roc_year_month_111(self):
        assert parse_date_from_filename(
            '111年04月民生月報.pdf'
        ) == datetime(2022, 4, 30)


class TestPattern8YYYYMM:
    """模式 8：6 碼 YYYYMM 月度報告，如 `202512`"""

    def test_yyyymm_basic(self):
        # 2025-12, 月底 2025-12-31
        assert parse_date_from_filename(
            '國泰承真監測月報202512.pdf'
        ) == datetime(2025, 12, 31)

    def test_yyyymm_does_not_eat_yyyymmdd(self):
        # YYYYMMDD 8 碼仍應走 pattern 1b，得到具體日期
        assert parse_date_from_filename('20260303_test.pdf') == datetime(2026, 3, 3)


class TestRegressionExistingPatterns:
    """確認新 pattern 不影響既有解析"""

    def test_roc_seven_digit(self):
        assert parse_date_from_filename('建案A_1150301.pdf') == datetime(2026, 3, 1)

    def test_iso_format(self):
        assert parse_date_from_filename('B-2026-04-15.pdf') == datetime(2026, 4, 15)

    def test_no_date_returns_none(self):
        assert parse_date_from_filename('A3003_樓梯剖面圖.pdf') is None
