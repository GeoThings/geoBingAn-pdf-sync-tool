"""Tests for CSV import in PermitSync."""
import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sync_permits import PermitSync


def _write_csv(tmpdir, content):
    path = os.path.join(tmpdir, 'permits.csv')
    with open(path, 'w', encoding='utf-8-sig') as f:
        f.write(content)
    return path


class TestLoadCsvList:
    def test_basic_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_csv(tmpdir, (
                'permit_no,source_url,name\n'
                '112建字第0238號,https://drive.google.com/drive/folders/ABC,全球人壽\n'
                '111建字第0071號,https://drive.google.com/drive/folders/DEF,連雲玥恒\n'
            ))
            ps = PermitSync(city={'csv_path': path, 'source_type': 'csv'})
            mapping = ps.load_csv_list()
            assert len(mapping) == 2
            assert mapping['112建字第0238號'] == 'https://drive.google.com/drive/folders/ABC'

    def test_empty_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_csv(tmpdir, 'permit_no,source_url,name\n')
            ps = PermitSync(city={'csv_path': path, 'source_type': 'csv'})
            mapping = ps.load_csv_list()
            assert len(mapping) == 0

    def test_missing_file(self):
        ps = PermitSync(city={'csv_path': '/nonexistent/path.csv', 'source_type': 'csv'})
        mapping = ps.load_csv_list()
        assert len(mapping) == 0

    def test_skips_empty_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_csv(tmpdir, (
                'permit_no,source_url,name\n'
                '112建字第0238號,https://drive.google.com/drive/folders/ABC,名稱\n'
                ',,\n'
                ',https://drive.google.com/drive/folders/DEF,\n'
                '111建字第0071號,,\n'
            ))
            ps = PermitSync(city={'csv_path': path, 'source_type': 'csv'})
            mapping = ps.load_csv_list()
            assert len(mapping) == 1
            assert '112建字第0238號' in mapping

    def test_strips_whitespace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write_csv(tmpdir, (
                'permit_no,source_url,name\n'
                '  112建字第0238號  ,  https://drive.google.com/drive/folders/ABC  , 名稱 \n'
            ))
            ps = PermitSync(city={'csv_path': path, 'source_type': 'csv'})
            mapping = ps.load_csv_list()
            assert '112建字第0238號' in mapping
            assert 'drive.google.com' in mapping['112建字第0238號']

    def test_utf8_bom(self):
        """Excel exports CSV with UTF-8 BOM."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'permits.csv')
            with open(path, 'w', encoding='utf-8-sig') as f:
                f.write('permit_no,source_url,name\n')
                f.write('112建字第0238號,https://example.com/ABC,測試\n')
            ps = PermitSync(city={'csv_path': path, 'source_type': 'csv'})
            mapping = ps.load_csv_list()
            assert len(mapping) == 1

    def test_source_type_routing(self):
        """Verify PermitSync picks CSV path based on source_type."""
        ps = PermitSync(city={'source_type': 'csv', 'csv_path': '/tmp/test.csv'})
        assert ps.source_type == 'csv'
        assert ps.csv_path == '/tmp/test.csv'

    def test_default_source_type_is_pdf(self):
        ps = PermitSync()
        assert ps.source_type == 'pdf'
