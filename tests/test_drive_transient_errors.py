"""Drive 呼叫碰到「連線層中斷」時的行為。

背景（2026-10-17、10-18 兩輪正式環境失敗，各一次，整輪沒有產出報告）：

    10/17  TimeoutError: The read operation timed out
           generate_permit_tracking_report.py:127 → list_all_subfolders
           → drive_utils.paginate_files_list
    10/18  ConnectionResetError: [Errno 54] Connection reset by peer
           generate_permit_tracking_report.py:157（手寫的 PDF 批次掃描）

兩次都死在步驟 3 的 Drive `files().list` socket 讀取上。原因不是「Drive 壞了」，
而是**步驟 3 的每一道錯誤處理都以 `HttpError` 為鍵**：`paginate_files_list` 的
重試、批次掃描的 fallback 觸發條件、fallback 自己的逐案 catch，三道都是。
連線層例外不是 `HttpError`，所以三道防線同時沒接到，例外一路往上把整輪帶走。

`HttpError` 是「Drive 呼叫失敗了」的**代理變數**——它只涵蓋 HTTP 狀態碼，
不涵蓋傳輸中斷。本檔把那個不變量寫成測試。
"""
import http.client
import os
import socket
import ssl
import sys

import pytest
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync import drive_utils
from geobingan_sync.drive_utils import paginate_files_list


class _FakeResp:
    def __init__(self, status=500, reason='err'):
        self.status = status
        self.reason = reason


def _http_error(status):
    return HttpError(_FakeResp(status), b'{}')


# 正式環境那兩個例外，照原樣重建
TIMEOUT_1017 = TimeoutError('The read operation timed out')
RESET_1018 = ConnectionResetError(54, 'Connection reset by peer')


class _ScriptedFiles:
    """script 元素：dict（回應）或 Exception（raise 後繼續吃下一個）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)

        class _Req:
            def execute(_self):
                if isinstance(item, Exception):
                    raise item
                return item

        return _Req()


class _ScriptedService:
    def __init__(self, script):
        self._files = _ScriptedFiles(script)

    def files(self):
        return self._files


# ---------------------------------------------------------------- 分類規則

class TestTransientClassification:
    """一條規則決定「值不值得重試」，呼叫點不各自判斷。"""

    @pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
    def test_retryable_http_status(self, status):
        assert drive_utils.is_transient_drive_error(_http_error(status)) is True

    @pytest.mark.parametrize('status', [400, 401, 403, 404])
    def test_client_side_http_status_is_not_transient(self, status):
        assert drive_utils.is_transient_drive_error(_http_error(status)) is False

    @pytest.mark.parametrize('exc', [
        TIMEOUT_1017,
        RESET_1018,
        socket.timeout('timed out'),
        ConnectionAbortedError(53, 'aborted'),
        BrokenPipeError(32, 'broken pipe'),
        socket.gaierror(8, 'nodename nor servname provided'),
        ssl.SSLEOFError('EOF occurred in violation of protocol'),
        http.client.IncompleteRead(b'partial'),
        http.client.BadStatusLine(''),
    ])
    def test_transport_failures_are_transient(self, exc):
        assert drive_utils.is_transient_drive_error(exc) is True

    def test_a_bad_certificate_is_not_transient(self):
        """重試不會讓憑證變對——這種要立刻失敗，不要浪費三輪退避。"""
        assert drive_utils.is_transient_drive_error(
            ssl.SSLCertVerificationError('certificate verify failed')) is False

    @pytest.mark.parametrize('exc', [ValueError('bad arg'), KeyError('nope'),
                                     FileNotFoundError(2, 'no such file')])
    def test_programming_errors_are_not_transient(self, exc):
        assert drive_utils.is_transient_drive_error(exc) is False


# ------------------------------------------------- paginate_files_list 重試

class TestPaginateRetriesTransport:

    def test_the_1017_timeout_is_retried_instead_of_raised(self):
        """10/17 的重現：讀取逾時一次，重試後成功。"""
        slept = []
        svc = _ScriptedService([TIMEOUT_1017, {'files': [{'id': 'a'}]}])
        items = paginate_files_list(svc, q='x', fields='files(id)',
                                    sleep=slept.append)
        assert [i['id'] for i in items] == ['a']
        assert len(svc._files.calls) == 2
        assert len(slept) == 1

    def test_the_1018_connection_reset_is_retried_instead_of_raised(self):
        """10/18 的重現（同一類故障、不同例外型別）。"""
        svc = _ScriptedService([RESET_1018, {'files': [{'id': 'a'}]}])
        items = paginate_files_list(svc, q='x', fields='files(id)',
                                    sleep=lambda _s: None)
        assert [i['id'] for i in items] == ['a']

    def test_transport_retries_are_bounded(self):
        """有限重試：1 次原呼叫 + 3 次重試後放棄，不會無限打下去。"""
        svc = _ScriptedService([TIMEOUT_1017] * 10)
        with pytest.raises(TimeoutError):
            paginate_files_list(svc, q='x', fields='files(id)',
                                sleep=lambda _s: None)
        assert len(svc._files.calls) == 4

    def test_still_fails_closed_and_never_returns_a_truncated_page_set(self):
        """重試用完要拋，不可以把已經收到的第一頁當成完整結果回傳。

        這是 #65／#67 的 bug class：靜默截斷比失敗更糟。
        """
        svc = _ScriptedService([
            {'files': [{'id': 'page1'}], 'nextPageToken': 't2'},
        ] + [RESET_1018] * 10)
        with pytest.raises(ConnectionResetError):
            paginate_files_list(svc, q='x', fields='files(id)',
                                sleep=lambda _s: None)

    def test_a_bad_certificate_fails_immediately(self):
        svc = _ScriptedService([ssl.SSLCertVerificationError('verify failed')] * 5)
        with pytest.raises(ssl.SSLError):
            paginate_files_list(svc, q='x', fields='files(id)',
                                sleep=lambda _s: None)
        assert len(svc._files.calls) == 1

    def test_one_page_shares_one_budget_across_error_types(self):
        """同一頁內混著來的暫時性失敗共用一份預算。

        不能變成「逾時三次、再加五百三次」——那是把一次抖動放大成九次呼叫。
        """
        svc = _ScriptedService([TIMEOUT_1017, _http_error(503), RESET_1018,
                                _http_error(500), {'files': []}])
        with pytest.raises(Exception):
            paginate_files_list(svc, q='x', fields='files(id)',
                                sleep=lambda _s: None)
        assert len(svc._files.calls) == 4

    def test_each_page_gets_its_own_budget(self):
        """每頁一份預算，整趟不共用。

        步驟 3 一趟要翻幾十頁；若整趟共用一份，長掃描會比單次呼叫更容易失敗，
        而長掃描正是我們要保住的那一個。這一條釘住「修 A 不要順手改掉 B」。
        """
        # 四頁、每頁各抖一次＝共 4 次失敗，比 retries=3 多。
        # 若預算整趟共用，第四頁就會失敗——這是分得出兩種語意的最小測資
        # （第一版只排三次失敗，剛好在共用預算內，注入因此沒被抓到）。
        svc = _ScriptedService([
            TIMEOUT_1017, {'files': [{'id': 'a'}], 'nextPageToken': 't2'},
            RESET_1018, {'files': [{'id': 'b'}], 'nextPageToken': 't3'},
            TIMEOUT_1017, {'files': [{'id': 'c'}], 'nextPageToken': 't4'},
            RESET_1018, {'files': [{'id': 'd'}]},
        ])
        items = paginate_files_list(svc, q='x', fields='files(id)',
                                    sleep=lambda _s: None)
        assert [i['id'] for i in items] == ['a', 'b', 'c', 'd']
        assert len(svc._files.calls) == 8


# -------------------------------------------- 步驟 3 的 PDF 掃描與 fallback

class _QueryRoutedFiles:
    """按 `q` 分派回應：批次掃描與逐資料夾掃描各有自己的 script。

    每個 script 用 pop(0) 消費；只剩最後一個時就固定回它——這樣
    「持續失敗」只要把例外放在最後即可。
    """

    def __init__(self, batch, per_folder=None):
        self.batch = list(batch)
        self.per_folder = {k: list(v) for k, v in (per_folder or {}).items()}
        self.calls = []

    def _take(self, script):
        return script.pop(0) if len(script) > 1 else script[0]

    def list(self, **kwargs):
        q = kwargs.get('q', '')
        self.calls.append(kwargs)
        if 'in parents' in q:
            folder_id = q.split("'")[1]
            script = self.per_folder.get(folder_id) or [{'files': []}]
            item = self._take(script)
        else:
            item = self._take(self.batch)

        class _Req:
            def execute(_self):
                if isinstance(item, Exception):
                    raise item
                return item

        return _Req()


class _RoutedService:
    def __init__(self, batch, per_folder=None):
        self._files = _QueryRoutedFiles(batch, per_folder)

    def files(self):
        return self._files


@pytest.fixture
def step3(monkeypatch):
    """載入步驟 3，並把兩個資料夾列舉換成固定的兩個建案。"""
    from geobingan_sync.steps import generate_permit_tracking_report as mod

    monkeypatch.setattr(drive_utils, 'list_top_level_folders',
                        lambda *a, **k: [
                            {'id': 'fid1', 'name': '110建字第0001號', 'modifiedTime': '2026-10-01'},
                            {'id': 'fid2', 'name': '110建字第0002號', 'modifiedTime': '2026-10-02'},
                        ])
    monkeypatch.setattr(drive_utils, 'list_all_subfolders', lambda *a, **k: {})
    monkeypatch.setattr(mod, 'SHARED_DRIVE_ID', 'drive-test')
    return mod


def _pdf(name, parent, mod_time='2026-10-20T10:00:00Z'):
    return {'name': name, 'modifiedTime': mod_time, 'parents': [parent]}


class TestStep3SurvivesTransportFailures:

    def test_the_1018_transport_error_is_retried_and_the_batch_scan_completes(
            self, step3, monkeypatch):
        """10/18 的重現：批次掃描中途連線被重設。

        修好之後這一輪應該**照常完成**，而且不必退回逐資料夾掃描
        （逐資料夾要打 400 次 API，是昂貴的保險，不該被一次抖動觸發）。
        """
        monkeypatch.setattr(drive_utils.time, 'sleep', lambda _s: None)
        svc = _RoutedService(batch=[
            RESET_1018,
            {'files': [_pdf('a.pdf', 'fid1'), _pdf('b.pdf', 'fid2')]},
        ])
        result = step3.scan_google_drive(svc)
        assert result['110建字第0001號']['pdf_count'] == 1
        assert result['110建字第0002號']['pdf_count'] == 1
        # 沒有任何逐資料夾查詢
        assert not any('in parents' in c.get('q', '') for c in svc._files.calls)

    def test_the_1017_timeout_in_subfolder_scan_is_handled_by_the_shared_paginator(
            self, monkeypatch):
        """10/17 死在 list_all_subfolders；那條路共用同一個翻頁器。

        這一條刻意不用 step3 fixture——fixture 把 list_all_subfolders 換掉了，
        用它就等於測一個假的（我第一版就是這樣寫錯的）。
        """
        monkeypatch.setattr(drive_utils.time, 'sleep', lambda _s: None)
        svc = _ScriptedService([TIMEOUT_1017,
                                {'files': [{'id': 'sub', 'parents': ['fid1']}]}])
        assert drive_utils.list_all_subfolders(svc, 'drive-test') == {'sub': 'fid1'}

    def test_a_persistent_batch_failure_still_falls_back_per_folder(
            self, step3, monkeypatch):
        """持續失敗才啟動昂貴的保險，而且保險本身要能跑完。"""
        monkeypatch.setattr(drive_utils.time, 'sleep', lambda _s: None)
        svc = _RoutedService(
            batch=[TIMEOUT_1017],
            per_folder={
                'fid1': [{'files': [_pdf('a.pdf', 'fid1'), _pdf('b.pdf', 'fid1')]}],
                'fid2': [{'files': [_pdf('c.pdf', 'fid2')]}],
            })
        result = step3.scan_google_drive(svc)
        assert result['110建字第0001號']['pdf_count'] == 2
        assert result['110建字第0002號']['pdf_count'] == 1

    def test_the_per_folder_fallback_paginates_beyond_one_page(
            self, step3, monkeypatch):
        """保險的用途是「確保完整性」，所以它自己不可以在第 1000 筆截斷。"""
        monkeypatch.setattr(drive_utils.time, 'sleep', lambda _s: None)
        svc = _RoutedService(
            batch=[TIMEOUT_1017],
            per_folder={
                'fid1': [
                    {'files': [_pdf('p1.pdf', 'fid1')], 'nextPageToken': 'n2'},
                    {'files': [_pdf('p2.pdf', 'fid1')]},
                ],
                'fid2': [{'files': []}],
            })
        result = step3.scan_google_drive(svc)
        assert result['110建字第0001號']['pdf_count'] == 2

    def test_an_unmeasurable_folder_is_not_recorded_as_zero(
            self, step3, monkeypatch):
        """沒量到 ≠ 0 份。

        `pdf_count == 0` 在下游會變成 `status = 'no_reports'`——報告上寫成
        「這個建案沒有監測報告」。對一個監測工具來說，那是對承造人的錯誤指控。
        量不到就要讓這一步失敗，不要發布一份說謊的報告。
        """
        monkeypatch.setattr(drive_utils.time, 'sleep', lambda _s: None)
        svc = _RoutedService(
            batch=[TIMEOUT_1017],
            per_folder={
                'fid1': [{'files': [_pdf('a.pdf', 'fid1')]}],
                'fid2': [RESET_1018],          # 這個資料夾怎麼重試都量不到
            })
        with pytest.raises(RuntimeError) as err:
            step3.scan_google_drive(svc)
        assert '110建字第0002號' in str(err.value)
        assert '未量到' in str(err.value)

    def test_a_programming_error_is_never_swallowed_by_the_fallback(
            self, step3, monkeypatch):
        """只有「Drive 呼叫失敗」才該觸發保險；自己寫錯的不要被吃掉。"""
        monkeypatch.setattr(drive_utils.time, 'sleep', lambda _s: None)
        svc = _RoutedService(batch=[ValueError('bad query')])
        with pytest.raises(ValueError):
            step3.scan_google_drive(svc)

    def test_the_batch_scan_goes_through_the_one_shared_paginator(
            self, step3, monkeypatch):
        """結構性：步驟 3 不准自己手寫 files().list。

        drive_utils 的模組註解本來就寫「一律走 paginate_files_list」，但步驟 3
        手寫了一份——於是 #65／#67 修過的截斷風險、以及這次的重試，全都繞過了。
        這裡給一個「被直接呼叫就爆」的 service，所以繞過翻頁器會立刻紅；
        第一版只比對查詢字串，太鬆，注入沒抓到。
        """
        seen = []

        def _spy(service, **kwargs):
            seen.append(kwargs.get("q", ""))
            return [_pdf("a.pdf", "fid1")]

        class _ExplodingFiles:
            def list(self, **kwargs):
                raise AssertionError(
                    "步驟 3 直接呼叫了 files().list，沒走共用翻頁器："
                    + kwargs.get("q", ""))

        class _ExplodingService:
            def files(self):
                return _ExplodingFiles()

        monkeypatch.setattr(drive_utils, "paginate_files_list", _spy)
        step3.scan_google_drive(_ExplodingService())

        pdf_queries = [q for q in seen if "application/pdf" in q]
        assert pdf_queries, "批次 PDF 掃描沒有走共用翻頁器"
        assert all("trashed=false" in q for q in pdf_queries), (
            "批次 PDF 查詢沒有排除垃圾桶——已刪除的 PDF 會被算進份數")
