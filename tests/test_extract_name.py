"""Tests for generate_permit_tracking_report.extract_name_from_filename()"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from generate_permit_tracking_report import extract_name_from_filename


class TestExtractNameFromFilename:
    # Basic extraction
    def test_report_with_name_and_date(self):
        assert extract_name_from_filename('連雲玥恒1150226報告.pdf') == '連雲玥恒'

    def test_name_with_dash_date_report(self):
        assert extract_name_from_filename('鼎真監測報告-芝山段1150226.pdf') == '鼎真'

    def test_safety_monitoring_report(self):
        result = extract_name_from_filename('1150226_凌霄好室_安全監測系統報告-0074.pdf')
        assert result == '凌霄好室'

    def test_observation_report(self):
        result = extract_name_from_filename('全球人壽新總部大樓0224觀測報告.pdf')
        assert result == '全球人壽新總部大樓'

    def test_western_date_format(self):
        result = extract_name_from_filename('112-0289安全觀測報告書-(2026-02-24 00_00).pdf')
        assert result == ''

    def test_measurement_report(self):
        result = extract_name_from_filename('國產建材實業B棟商業辦公大樓-115年02月24日量測報告.pdf')
        assert result == '國產建材實業B棟商業辦公大樓'

    # Parenthesized name at start
    def test_paren_name_prefix(self):
        assert extract_name_from_filename('(嘉鎷)115.01監測月報.pdf') == '嘉鎷'

    def test_fullwidth_paren_name(self):
        assert extract_name_from_filename('（嘉鎷）115.01監測月報.pdf') == '嘉鎷'

    # Project interval reports
    def test_project_interval(self):
        result = extract_name_from_filename('(基地)六福豐融商業大樓新建工程-專案區間報告書-2026-02-23 00_00-2026-03-01 00_00 (1).pdf')
        assert '六福豐融' in result

    # Edge cases — should return empty
    def test_generic_name_filtered(self):
        assert extract_name_from_filename('觀測紀錄.pdf') == ''

    def test_too_short(self):
        assert extract_name_from_filename('A.pdf') == ''

    def test_only_dates(self):
        assert extract_name_from_filename('1150226.pdf') == ''

    def test_only_numbers(self):
        assert extract_name_from_filename('12345678.pdf') == ''

    def test_permit_number_only(self):
        assert extract_name_from_filename('112建字第0238號.pdf') == ''

    # Should not crash
    def test_empty_filename(self):
        result = extract_name_from_filename('')
        assert isinstance(result, str)

    def test_just_pdf_extension(self):
        result = extract_name_from_filename('.pdf')
        assert isinstance(result, str)
