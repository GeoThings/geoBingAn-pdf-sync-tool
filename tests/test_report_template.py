"""Smoke tests for report_template.py

Ensures generate_html_report and generate_csv_report run without crashing
on representative data. These tests caught the missing datetime import in PR #19.
"""
import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from report_template import generate_html_report, generate_csv_report


def _sample_permit_data():
    return {
        '112建字第0238號': {
            'drive_count': 15,
            'system_count': 12,
            'latest_report': '2026-04-10T08:00:00Z',
            'days_since_update': 6,
            'status': 'in_progress',
        },
        '111建字第0071號': {
            'drive_count': 0,
            'system_count': 0,
            'latest_report': None,
            'days_since_update': '',
            'status': 'no_reports',
        },
        '110建字第0325號': {
            'drive_count': 30,
            'system_count': 30,
            'latest_report': '2026-03-01T00:00:00Z',
            'days_since_update': 46,
            'status': 'completed_project',
        },
    }


def _sample_non_google():
    return [
        {'permit': '111建字第0093號', 'cloud': 'OneDrive', 'url': 'https://onedrive.example.com'},
    ]


def _sample_alert_data():
    return {
        '112建字第0238號': {
            'warning_count': 2,
            'danger_count': 1,
            'latest_alert_date': '2026-04-09T12:00:00Z',
        },
    }


def _sample_permit_names():
    return {
        '112建字第0238號': '全球人壽新總部大樓',
        '110建字第0325號': '凌霄好室',
    }


class TestGenerateHtmlReport:
    def test_basic_generation(self):
        html = generate_html_report(
            _sample_permit_data(),
            _sample_non_google(),
        )
        assert isinstance(html, str)
        assert len(html) > 1000
        assert '<html' in html
        assert '</html>' in html

    def test_with_all_optional_params(self):
        html = generate_html_report(
            _sample_permit_data(),
            _sample_non_google(),
            alert_data=_sample_alert_data(),
            permit_names=_sample_permit_names(),
        )
        assert '全球人壽新總部大樓' in html
        assert '112建字第0238號' in html

    def test_empty_data(self):
        html = generate_html_report({}, [])
        assert isinstance(html, str)
        assert '<html' in html

    def test_writes_to_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'report.html')
            html = generate_html_report(
                _sample_permit_data(),
                _sample_non_google(),
                output_path=path,
            )
            assert os.path.exists(path)
            with open(path, encoding='utf-8') as f:
                assert f.read() == html

    def test_no_file_when_no_path(self):
        html = generate_html_report(_sample_permit_data(), _sample_non_google())
        assert isinstance(html, str)

    def test_html_escaping(self):
        """Permit names with special chars should be escaped."""
        names = {'112建字第0238號': '<script>alert("xss")</script>'}
        html = generate_html_report(
            _sample_permit_data(),
            _sample_non_google(),
            permit_names=names,
        )
        assert '<script>alert' not in html
        assert '&lt;script&gt;' in html


class TestGenerateCsvReport:
    def test_basic_generation(self):
        csv = generate_csv_report(
            _sample_permit_data(),
            _sample_non_google(),
        )
        assert isinstance(csv, str)
        lines = csv.strip().split('\n')
        assert len(lines) == 4  # header + 3 permits

    def test_header_columns(self):
        csv = generate_csv_report(_sample_permit_data(), _sample_non_google())
        header = csv.split('\n')[0]
        assert '建照字號' in header
        assert '建案名稱' in header

    def test_with_alert_data(self):
        csv = generate_csv_report(
            _sample_permit_data(),
            _sample_non_google(),
            alert_data=_sample_alert_data(),
            permit_names=_sample_permit_names(),
        )
        assert '全球人壽新總部大樓' in csv

    def test_empty_data(self):
        csv = generate_csv_report({}, [])
        lines = csv.strip().split('\n')
        assert len(lines) == 1  # header only

    def test_writes_to_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'report.csv')
            csv = generate_csv_report(
                _sample_permit_data(),
                _sample_non_google(),
                output_path=path,
            )
            assert os.path.exists(path)
            with open(path, encoding='utf-8-sig') as f:
                assert f.read() == csv
