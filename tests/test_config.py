"""Tests for config utility functions."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import escape_drive_query


class TestEscapeDriveQuery:
    def test_normal_string(self):
        assert escape_drive_query('normal_file.pdf') == 'normal_file.pdf'

    def test_single_quote(self):
        assert escape_drive_query("test'file.pdf") == "test\\'file.pdf"

    def test_backslash(self):
        assert escape_drive_query('test\\file.pdf') == 'test\\\\file.pdf'

    def test_both(self):
        assert escape_drive_query("a\\'b") == "a\\\\\\'b"

    def test_empty(self):
        assert escape_drive_query('') == ''

    def test_chinese_characters(self):
        assert escape_drive_query('建案監測報告.pdf') == '建案監測報告.pdf'

    def test_multiple_quotes(self):
        assert escape_drive_query("a'b'c") == "a\\'b\\'c"
