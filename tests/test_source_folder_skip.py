"""fetch_source_folder_names 已知 404 快取跳過行為。

背景：政府 PDF 凍結後仍列著已被監測公司刪除的 Drive 資料夾，每次 build 都對
這些 folder_id 空打 files().get 只為再拿一次 404，log 噪音持續累積。Drive
folder_id 一旦 404 即永久失效，故上次已記錄 gov_pdf_url_status=='404' 者本次
直接沿用、不重打 API、不噴警告。此測試固定該行為。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geobingan_sync.steps.match_permits import fetch_source_folder_names


class _TrackingService:
    """記錄被查詢的 fileId；'deadid' 回 404，其餘回可清理的資料夾名。"""

    def __init__(self):
        self.queried = []
        from googleapiclient.errors import HttpError

        class _Resp:
            status = 404
            reason = 'notFound'

        self._err = HttpError(_Resp(), b'nf')

    def files(self):
        svc = self

        class _F:
            def get(self, fileId, **kw):
                svc.queried.append(fileId)

                class _R:
                    def execute(_r):
                        if fileId == 'deadid':
                            raise svc._err
                        return {'name': '力麒松江總部大樓'}
                return _R()
        return _F()


def _gov():
    return {
        '111建字第0058號': {'source_folder_id': 'deadid'},   # 上次已 404
        '111建字第0140號': {'source_folder_id': 'aliveid'},  # 存活
    }


def test_known_dead_folder_is_skipped(capsys):
    svc = _TrackingService()
    prior = {'111建字第0058號': {'gov_pdf_url_status': '404'}}

    names, statuses = fetch_source_folder_names(_gov(), svc, prior_registry=prior)

    # 已知 404 的建照不得再打 API
    assert 'deadid' not in svc.queried
    # 存活的仍要查詢並取名
    assert 'aliveid' in svc.queried
    assert statuses['111建字第0058號'] == '404'   # 沿用
    assert statuses['111建字第0140號'] == 'alive'
    assert names['111建字第0140號'] == '力麒松江總部大樓'
    # 不得再噴失效警告
    assert '讀取失敗' not in capsys.readouterr().out


def test_without_prior_status_still_queries_and_marks_404(capsys):
    """回歸：沒有先前 404 記錄時，維持原行為（查詢 + 標 404 + 警告）。"""
    svc = _TrackingService()

    names, statuses = fetch_source_folder_names(_gov(), svc, prior_registry={})

    assert 'deadid' in svc.queried   # 無快取 → 照打
    assert statuses['111建字第0058號'] == '404'
    assert '讀取失敗' in capsys.readouterr().out
