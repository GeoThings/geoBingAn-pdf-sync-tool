"""Tests for monthly activity trend helpers in weekly_snapshot.

Covers the pure-function helpers added in PR #35 — _months_back and
_bin_pdfs_by_report_month — without touching state files or notify channels.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from weekly_snapshot import _bin_pdfs_by_report_month, _months_back


class TestMonthsBack:
    def test_one_month_within_year(self):
        assert _months_back(2026, 5, 1) == (2026, 4)

    def test_cross_year_boundary_one(self):
        assert _months_back(2026, 1, 1) == (2025, 12)

    def test_cross_year_boundary_three(self):
        assert _months_back(2026, 1, 3) == (2025, 10)

    def test_zero_no_op(self):
        assert _months_back(2026, 5, 0) == (2026, 5)

    def test_twelve_months_back(self):
        assert _months_back(2026, 5, 12) == (2025, 5)


class TestBinPdfsByReportMonth:
    def test_roc_seven_digit_filename(self):
        # 民國 1150301 = 2026-03-01
        result = _bin_pdfs_by_report_month([{'name': '建案A_1150301.pdf'}])
        assert result.get((2026, 3)) == 1

    def test_iso_filename(self):
        result = _bin_pdfs_by_report_month([{'name': 'B-2026-04-15.pdf'}])
        assert result.get((2026, 4)) == 1

    def test_unparseable_filename_skipped(self):
        result = _bin_pdfs_by_report_month([{'name': 'no-date-here.pdf'}])
        assert sum(result.values()) == 0

    def test_missing_name_field_safe(self):
        result = _bin_pdfs_by_report_month([{}])
        assert sum(result.values()) == 0

    def test_mixed_formats_aggregate(self):
        pdfs = [
            {'name': '建案A_1150301.pdf'},   # 2026-03
            {'name': '建案A_1150315.pdf'},   # 2026-03
            {'name': 'B-2026-04-02.pdf'},    # 2026-04
            {'name': 'no-date.pdf'},
        ]
        result = _bin_pdfs_by_report_month(pdfs)
        assert result.get((2026, 3)) == 2
        assert result.get((2026, 4)) == 1
        assert sum(result.values()) == 3
