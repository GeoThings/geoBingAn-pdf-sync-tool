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


# ---------- 跨三輪：一次暫時性 error 不得截斷世系（review P2，2026-09-29）----------
#
# 舊版讓 error 覆寫 gov_pdf_url_status，於是 404 → error → alive 這條路上，
# 第三輪的 prior 已經不是 404，復活既不記 revived_at 也不公告——「復活不能
# 無聲」的保證被一次 API 逾時永久截斷。同一個根因也讓 alive → error → 404
# 漏掉新失效告警。修法是同一條規則：error 無定論，不得覆寫有定論的狀態。
#
# 三輪都用**同一個 folder ID**：換了 ID 就不是同一個資料夾復活，那條路徑由
# test_folder_id_changed_is_revalidated 管。

from geobingan_sync.steps.match_permits import apply_url_probe, detect_folder_deaths

ROUND = [NOW - timedelta(days=16), NOW - timedelta(days=8), NOW]
FID = 'samefolder'


class _Svc:
    """對任何 fileId 都給同一種結果的假 Drive service。"""

    def __init__(self, outcome):
        self.outcome = outcome
        self.queried = []
        from googleapiclient.errors import HttpError

        class _Resp:
            status = 404
            reason = 'notFound'
        self._nf = HttpError(_Resp(), b'nf')

    def files(self):
        outer = self

        class _F:
            def get(self, fileId=None, **kw):
                outer.queried.append(fileId)

                class _R:
                    def execute(self_inner):
                        if outer.outcome == '404':
                            raise outer._nf
                        if outer.outcome == 'error':
                            raise RuntimeError('timeout')
                        return {'name': '力麒松江總部大樓'}
                return _R()
        return _F()


def _round(entry, outcome, now):
    """跑一輪：探測 → 依規則寫回 entry。回傳本輪原始探測結果。"""
    gov = {'P': {'source_folder_id': FID}}
    prior = {'P': dict(entry)}
    svc = _Svc(outcome)
    _, statuses, checked = fetch_source_folder_names(gov, svc, prior_registry=prior, now=now)
    apply_url_probe(entry, statuses['P'], checked_at=checked.get('P'), now=now.isoformat())
    return statuses['P'], svc


def test_dead_then_error_then_alive_is_still_announced(capsys):
    """404 → error → alive：中間那次逾時不可讓復活變成無聲。"""
    entry = {'gov_pdf_url_status': '404', 'source_url': _url(FID),
             'gov_pdf_url_checked_at': ROUND[0].isoformat()}

    # 第二輪：到期重驗，但 API 掛了 → 無定論
    outcome, svc = _round(entry, 'error', ROUND[1])
    assert outcome == 'error' and FID in svc.queried
    assert entry['gov_pdf_url_status'] == '404', 'error 不可覆寫已確認的 404'
    assert entry['gov_pdf_url_probe_error_at'] == ROUND[1].isoformat()
    assert entry['gov_pdf_url_checked_at'] == ROUND[0].isoformat(), '無定論不可延後下次重驗'

    # 第三輪：同一個資料夾恢復分享
    capsys.readouterr()
    outcome, _ = _round(entry, 'alive', ROUND[2])
    assert outcome == 'alive'
    assert entry['gov_pdf_url_status'] == 'alive'
    assert entry['gov_pdf_url_revived_at'] == ROUND[2].isoformat(), '復活必須留下紀錄'
    assert '已恢復' in capsys.readouterr().out, '復活必須公告'
    assert 'gov_pdf_url_probe_error_at' not in entry, '有定論後要清掉探測失敗標記'


def test_alive_then_error_then_404_is_still_a_death():
    """alive → error → 404：同一個根因的反方向，新失效不可被逾時吃掉。"""
    entry = {'gov_pdf_url_status': 'alive', 'source_url': _url(FID),
             'gov_pdf_url_checked_at': ROUND[0].isoformat()}
    prior_snapshot = {'P': entry['gov_pdf_url_status']}   # build_registry 開頭取的快照

    assert _round(entry, 'error', ROUND[1])[0] == 'error'
    assert entry['gov_pdf_url_status'] == 'alive', 'error 不可覆寫已確認的 alive'

    # 第三輪真的死了。快照取自「上一次有定論的狀態」，故仍是 alive。
    assert _round(entry, '404', ROUND[2])[0] == '404'
    deaths = detect_folder_deaths(prior_snapshot, {'P': entry})
    assert [d['permit'] for d in deaths] == ['P'], '暫時性錯誤不可讓真正的失效漏報'


def test_error_alone_never_writes_status():
    """從未有過定論的建案，error 也不可憑空寫出一個狀態。"""
    entry = {'source_url': _url(FID)}
    assert _round(entry, 'error', ROUND[2])[0] == 'error'
    assert 'gov_pdf_url_status' not in entry
    assert 'gov_pdf_url_checked_at' not in entry
    assert entry['gov_pdf_url_probe_error_at'] == ROUND[2].isoformat()


def test_api_error_is_not_silenced_for_dead_folders(capsys):
    """已知失效的重驗遇到 API 故障要印出來：快取要消除的是重複 404，不是故障。"""
    entry = {'gov_pdf_url_status': '404', 'source_url': _url(FID),
             'gov_pdf_url_checked_at': ROUND[0].isoformat()}
    _round(entry, 'error', ROUND[1])
    assert '讀取失敗' in capsys.readouterr().out
