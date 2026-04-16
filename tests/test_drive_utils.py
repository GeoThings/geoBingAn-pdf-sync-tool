"""Tests for drive_utils shared functions."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from drive_utils import build_folder_resolver


class TestBuildFolderResolver:
    def test_direct_match(self):
        top = {'folder_a': 'permit_001'}
        resolve = build_folder_resolver(top, {})
        assert resolve('folder_a') == 'permit_001'

    def test_one_level_deep(self):
        top = {'folder_a': 'permit_001'}
        subs = {'sub_b': 'folder_a'}
        resolve = build_folder_resolver(top, subs)
        assert resolve('sub_b') == 'permit_001'

    def test_two_levels_deep(self):
        top = {'folder_a': 'permit_001'}
        subs = {'sub_c': 'sub_b', 'sub_b': 'folder_a'}
        resolve = build_folder_resolver(top, subs)
        assert resolve('sub_c') == 'permit_001'

    def test_max_depth_exceeded(self):
        top = {'root': 'permit_001'}
        subs = {f'level_{i}': f'level_{i-1}' for i in range(1, 10)}
        subs['level_0'] = 'root'
        resolve = build_folder_resolver(top, subs, max_depth=2)
        # level_8 is 9 hops from root, exceeds max_depth without cached intermediates
        assert resolve('level_8') is None

    def test_unknown_folder(self):
        resolve = build_folder_resolver({}, {})
        assert resolve('unknown') is None

    def test_caching(self):
        top = {'folder_a': 'permit_001'}
        subs = {'sub_b': 'folder_a'}
        resolve = build_folder_resolver(top, subs)
        assert resolve('sub_b') == 'permit_001'
        assert resolve('sub_b') == 'permit_001'  # cached

    def test_warm_cache_extends_depth(self):
        """Production pattern: resolve all subfolders sequentially, cache extends effective reach."""
        top = {'root': 'permit_001'}
        subs = {f'level_{i}': f'level_{i-1}' for i in range(1, 8)}
        subs['level_0'] = 'root'
        resolve = build_folder_resolver(top, subs, max_depth=3)
        # Resolve from shallowest to deepest — each caches and extends reach
        for i in range(8):
            resolve(f'level_{i}')
        assert resolve('level_7') == 'permit_001'

    def test_multiple_permits(self):
        top = {'fa': 'p1', 'fb': 'p2'}
        subs = {'sa': 'fa', 'sb': 'fb'}
        resolve = build_folder_resolver(top, subs)
        assert resolve('sa') == 'p1'
        assert resolve('sb') == 'p2'
