"""fetch_source_folder_names 已知 404 快取跳過行為。

背景：政府 PDF 凍結後仍列著已被監測公司刪除的 Drive 資料夾，每次 build 都對
這些 folder_id 空打 files().get 只為再拿一次 404，log 噪音持續累積。故上次已記錄
gov_pdf_url_status=='404'（且為同一 folder ID）者本次沿用、不重打 API、不噴警告。

但沿用是有期限的：404 也可能只是公開分享被取消，隨時會改回來。超過
RECHECK_DEAD_AFTER_DAYS 天就必須重驗，恢復了要偵測得到（見檔案下半）。

快取鍵必須綁 folder ID：同一建照日後換新 folder 時，舊 404 不得套到新 ID。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta

from geobingan_sync.steps.match_permits import fetch_source_folder_names

NOW = datetime(2026, 9, 29)
FRESH = (NOW - timedelta(days=1)).isoformat()      # 未到重驗期
STALE = (NOW - timedelta(days=8)).isoformat()      # 已過重驗期


def _url(fid):
    return f'https://drive.google.com/drive/folders/{fid}'


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
    # 快取需綁「同一 folder ID」：prior source_url 指向同一個 deadid。
    prior = {'111建字第0058號': {'gov_pdf_url_status': '404', 'source_url': _url('deadid'),
                                 'gov_pdf_url_checked_at': FRESH}}

    names, statuses, checked = fetch_source_folder_names(_gov(), svc, prior_registry=prior, now=NOW)

    # 已知 404 的建照不得再打 API
    assert 'deadid' not in svc.queried
    # 存活的仍要查詢並取名
    assert 'aliveid' in svc.queried
    assert statuses['111建字第0058號'] == '404'   # 沿用
    assert statuses['111建字第0140號'] == 'alive'
    assert names['111建字第0140號'] == '力麒松江總部大樓'
    # 不得再噴失效警告
    assert '讀取失敗' not in capsys.readouterr().out
    assert '111建字第0058號' not in checked, '被跳過的不可更新時間戳，否則永遠不會到期'


def test_without_prior_status_still_queries_and_marks_404(capsys):
    """回歸：沒有先前 404 記錄時，維持原行為（查詢 + 標 404 + 警告）。"""
    svc = _TrackingService()

    names, statuses, checked = fetch_source_folder_names(_gov(), svc, prior_registry={}, now=NOW)

    assert 'deadid' in svc.queried   # 無快取 → 照打
    assert statuses['111建字第0058號'] == '404'
    assert '讀取失敗' in capsys.readouterr().out


def test_folder_id_changed_is_revalidated(capsys):
    """回歸（review P2）：同一建照 source_url 換成新 folder ID 時，
    舊 404 快取不得套用，必須重打 API 驗證新 ID。"""
    svc = _TrackingService()
    # 政府資料現在指向新的、存活的 folder ID
    gov = {'111建字第0058號': {'source_folder_id': 'newliveid'}}
    # 但 registry 舊記錄是另一個已死的 folder ID + 404
    prior = {'111建字第0058號': {'gov_pdf_url_status': '404', 'source_url': _url('olddeadid'),
                                 'gov_pdf_url_checked_at': FRESH}}

    names, statuses, checked = fetch_source_folder_names(gov, svc, prior_registry=prior, now=NOW)

    # 新 ID 必須被查詢（不得因舊 404 而永久跳過）
    assert 'newliveid' in svc.queried
    assert statuses['111建字第0058號'] == 'alive'
    assert names['111建字第0058號'] == '力麒松江總部大樓'


# ---------- 定期重驗：404 不是永久事實（2026-09-29）----------
#
# 舊註解寫「該 ID 不會復活」，但 API 回 404 有兩種成因：資料夾被刪除，或**公開分享
# 被取消**。後者隨時可以改回來，而我們一旦標 404 就再也不重驗，恢復永遠看不到。
# 實測當時 66 個建案被標 404，其中 38 個我方已有 PDF、合計 2,107 份。

def test_dead_folder_is_rechecked_after_interval():
    svc = _TrackingService()
    prior = {'111建字第0058號': {'gov_pdf_url_status': '404', 'source_url': _url('deadid'),
                                 'gov_pdf_url_checked_at': STALE}}
    names, statuses, checked = fetch_source_folder_names(_gov(), svc, prior_registry=prior, now=NOW)
    assert 'deadid' in svc.queried, '超過重驗期就必須重打 API'
    assert statuses['111建字第0058號'] == '404'
    assert checked['111建字第0058號'] == NOW.isoformat(), '重驗後要更新時間戳'


def test_dead_folder_without_timestamp_is_rechecked_immediately():
    """舊資料沒有時間戳 → 立刻重驗一次，把基準補上。"""
    svc = _TrackingService()
    prior = {'111建字第0058號': {'gov_pdf_url_status': '404', 'source_url': _url('deadid')}}
    names, statuses, checked = fetch_source_folder_names(_gov(), svc, prior_registry=prior, now=NOW)
    assert 'deadid' in svc.queried
    assert checked['111建字第0058號'] == NOW.isoformat()


def test_corrupt_timestamp_triggers_recheck():
    svc = _TrackingService()
    prior = {'111建字第0058號': {'gov_pdf_url_status': '404', 'source_url': _url('deadid'),
                                 'gov_pdf_url_checked_at': 'not-a-date'}}
    _, _, checked = fetch_source_folder_names(_gov(), svc, prior_registry=prior, now=NOW)
    assert 'deadid' in svc.queried and '111建字第0058號' in checked


def test_revived_folder_is_detected_and_announced(capsys):
    """分享改回來 → 必須偵測到並明講，否則等於永遠失聯。"""
    svc = _TrackingService()
    gov = {'111建字第0140號': {'source_folder_id': 'aliveid'}}
    prior = {'111建字第0140號': {'gov_pdf_url_status': '404', 'source_url': _url('aliveid'),
                                 'gov_pdf_url_checked_at': STALE}}
    names, statuses, checked = fetch_source_folder_names(gov, svc, prior_registry=prior, now=NOW)
    assert statuses['111建字第0140號'] == 'alive'
    assert '已恢復' in capsys.readouterr().out


def test_recheck_of_dead_folder_stays_quiet(capsys):
    """重驗仍失效時不可再噴警告——那正是當初加快取要消除的噪音。"""
    svc = _TrackingService()
    prior = {'111建字第0058號': {'gov_pdf_url_status': '404', 'source_url': _url('deadid'),
                                 'gov_pdf_url_checked_at': STALE}}
    fetch_source_folder_names(_gov(), svc, prior_registry=prior, now=NOW)
    assert '讀取失敗' not in capsys.readouterr().out


def test_error_status_does_not_update_timestamp():
    """error＝狀態未知，不可當成「已驗過」而延後下次重驗。"""
    class _ErrSvc(_TrackingService):
        def files(self):
            outer = self

            class _F:
                def get(self, fileId=None, **kw):
                    outer.queried.append(fileId)

                    class _E:
                        def execute(self_inner):
                            raise RuntimeError('network')
                    return _E()
            return _F()
    svc = _ErrSvc()
    prior = {'111建字第0058號': {'gov_pdf_url_status': '404', 'source_url': _url('deadid'),
                                 'gov_pdf_url_checked_at': STALE}}
    _, statuses, checked = fetch_source_folder_names(_gov(), svc, prior_registry=prior, now=NOW)
    assert statuses['111建字第0058號'] == 'error'
    assert '111建字第0058號' not in checked
