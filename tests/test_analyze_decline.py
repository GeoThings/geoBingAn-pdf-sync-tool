"""Tests for analyze_decline pure helpers (PR #40)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analyze_decline import _is_real_site, _months_back, find_decline_candidates


class TestMonthsBack:
    def test_within_year(self):
        assert _months_back(2026, 5, 1) == (2026, 4)

    def test_cross_year(self):
        assert _months_back(2026, 1, 3) == (2025, 10)

    def test_zero_no_op(self):
        assert _months_back(2026, 5, 0) == (2026, 5)


class TestIsRealSite:
    """過濾時間分類資料夾（115年1月、2026年03月、11502 等）。"""

    def test_real_site_keywords(self):
        assert _is_real_site('112建字第0126號揚昇君悅觀測報告')
        assert _is_real_site('建號112-0289基地觀測系統數據')
        assert _is_real_site('松智路新建工程')

    def test_time_organization_folder_rejected(self):
        assert not _is_real_site('115年1月')
        assert not _is_real_site('2026年03月')
        assert not _is_real_site('11502')
        assert not _is_real_site('115.01')
        assert not _is_real_site('115年')

    def test_empty(self):
        assert not _is_real_site('')


class TestFindDeclineCandidates:
    def _make_pdf(self, folder, name, modified='2026-03-15T00:00:00.000Z', folder_id='fid'):
        return {'folder_name': folder, 'name': name, 'modifiedTime': modified, 'folder_id': folder_id}

    def test_candidate_qualifies_when_prior_active_target_zero(self):
        # 構造：site A 在 2026-01/02/03 各 3 份，2026-04 = 0
        pdfs = []
        for day in [5, 10, 15]:
            pdfs.append(self._make_pdf('112建字第0001號A工程', f'A_115010{day}.pdf'))
            pdfs.append(self._make_pdf('112建字第0001號A工程', f'A_115020{day}.pdf'))
            pdfs.append(self._make_pdf('112建字第0001號A工程', f'A_115030{day}.pdf'))

        result = find_decline_candidates(pdfs, 2026, 4, top=10)
        assert len(result) == 1
        assert result[0]['folder'] == '112建字第0001號A工程'
        assert result[0]['prior_total'] == 9

    def test_time_folder_excluded(self):
        # 構造：時間分類資料夾「2026年03月」即使滿足數量條件也不該入選
        pdfs = []
        for d in ['1150301', '1150305', '1150310']:
            pdfs.append(self._make_pdf('2026年03月', f'X_{d}.pdf'))
            pdfs.append(self._make_pdf('2026年02月', f'X_{d.replace("03", "02")}.pdf'))
            pdfs.append(self._make_pdf('2026年01月', f'X_{d.replace("03", "01")}.pdf'))
        result = find_decline_candidates(pdfs, 2026, 4, top=10)
        assert result == []

    def test_target_with_recent_activity_excluded(self):
        # 4 月有報告 → 不算「歸零」
        pdfs = []
        for d in ['1150105', '1150205', '1150305', '1150405']:
            pdfs.append(self._make_pdf('112建字第0002號B工程', f'B_{d}.pdf'))
        result = find_decline_candidates(pdfs, 2026, 4, top=10, min_prior_per_month=1)
        assert result == []

    def test_top_limits_results(self):
        pdfs = []
        for i in range(10):
            for d in ['1150105', '1150115', '1150205', '1150215', '1150305', '1150315']:
                pdfs.append(self._make_pdf(f'112建字第000{i}號工程site{i}', f'f_{d}.pdf', folder_id=f'fid{i}'))
        result = find_decline_candidates(pdfs, 2026, 4, top=3)
        assert len(result) == 3
