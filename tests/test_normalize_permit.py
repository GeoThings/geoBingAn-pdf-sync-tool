"""Tests for match_permits.normalize_permit()"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from match_permits import normalize_permit


class TestNormalizePermit:
    def test_standard_format(self):
        assert normalize_permit('112建字第0238號') == '112建字第0238號'

    def test_without_trailing_號(self):
        assert normalize_permit('112建字第0238') == '112建字第0238號'

    def test_leading_zeros_stripped(self):
        assert normalize_permit('112建字第00238號') == '112建字第0238號'

    def test_short_number_zero_padded(self):
        assert normalize_permit('112建字第238號') == '112建字第0238號'

    def test_spaces_between_parts(self):
        assert normalize_permit('112 建 字 第 0238 號') == '112建字第0238號'

    def test_two_digit_year(self):
        assert normalize_permit('99建字第0001號') == '99建字第0001號'

    def test_three_digit_year(self):
        assert normalize_permit('110建字第0325號') == '110建字第0325號'

    def test_five_digit_number(self):
        assert normalize_permit('111建字第5688號') == '111建字第5688號'

    def test_embedded_in_text(self):
        assert normalize_permit('建照字號112建字第0238號新建工程') == '112建字第0238號'

    def test_without_字(self):
        assert normalize_permit('112建第0238號') == '112建字第0238號'

    def test_no_match(self):
        assert normalize_permit('不是建照號碼') is None

    def test_empty_string(self):
        assert normalize_permit('') is None

    def test_only_numbers(self):
        assert normalize_permit('12345') is None
