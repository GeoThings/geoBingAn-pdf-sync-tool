"""Smoke tests for tools/cleanup_stale_folders.py（PR #74 review P1）。

背景：工具搬進 tools/ 後 REGISTRY_FILE 曾指向不存在的 tools/state/，
--help 驗證觸不到讀檔路徑所以沒抓到。此處固定：
1. REGISTRY_FILE 必須落在 repo 的 state/ 下
2. dry-run 真的讀 registry 並回報失效項目（實際執行路徑 smoke）
"""
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync import REPO_ROOT

_TOOL = REPO_ROOT / 'tools' / 'cleanup_stale_folders.py'
spec = importlib.util.spec_from_file_location('cleanup_stale_folders', _TOOL)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def test_registry_file_points_into_repo_state():
    assert tool.REGISTRY_FILE == REPO_ROOT / 'state' / 'permit_registry.json'
    # 絕不可指向 tools/state（搬移前的 bug）
    assert 'tools' not in tool.REGISTRY_FILE.parts


class _FakeService:
    """bad-id → 404（is_folder_alive False），其餘存活。"""

    def __init__(self):
        from googleapiclient.errors import HttpError

        class _Resp:
            status = 404
            reason = 'notFound'

        self._err = HttpError(_Resp(), b'nf')

    def files(self):
        svc = self

        class _F:
            def get(self, fileId, **kw):
                class _R:
                    def execute(_r):
                        if fileId == 'deadid':
                            raise svc._err
                        return {'id': fileId}
                return _R()
        return _F()


def test_dry_run_reads_registry_and_reports_dead(tmp_path, monkeypatch, capsys):
    reg = tmp_path / 'permit_registry.json'
    reg.write_text(json.dumps({
        '110建字第0001號': {'source_url': 'https://drive.google.com/drive/folders/deadid'},
        '110建字第0002號': {'source_url': 'https://drive.google.com/drive/folders/aliveid'},
        '110建字第0003號': {'name': '無 URL，應被跳過'},
    }, ensure_ascii=False), encoding='utf-8')

    monkeypatch.setattr(tool, 'REGISTRY_FILE', reg)
    import geobingan_sync.steps.sync_permits as sp
    monkeypatch.setattr(sp, 'get_drive_service', lambda: _FakeService())
    monkeypatch.setattr(sys, 'argv', ['cleanup_stale_folders.py'])  # dry-run

    tool.main()

    out = capsys.readouterr().out
    assert '含 Drive folder URL: 2' in out
    assert '失效（404）共 1 筆' in out
    assert '110建字第0001號' in out
    # dry-run 不得改動 registry
    data = json.loads(reg.read_text(encoding='utf-8'))
    assert 'source_url' in data['110建字第0001號']
