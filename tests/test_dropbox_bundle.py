"""Dropbox 整包來源：安全解壓、取用間隔、暫存檔清理。

2026-10-01 實測，Dropbox 沒有便宜的列檔方式：檔案清單不在靜態 HTML（連已知檔名
都搜不到），zip 下載是 chunked、**沒有** Content-Length／ETag／Last-Modified，
所以無法判斷有沒有新檔。逆向內部列檔 API 太脆，申請官方 app 要多養憑證。

所以只能 `?dl=1` 一次拿整包（實測 284MB／10.6MB／17.0MB，共 269 份 PDF），
代價是頻寬，用取用間隔壓下來。整包來自外部，解壓必須擋路徑穿越與壓縮炸彈。
"""
import io
import json
import os
import tempfile
import zipfile
from datetime import datetime, timedelta

import pytest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.source_adapters import ADAPTERS, AdapterError, find_adapter
from geobingan_sync.source_adapters.base import (MAX_BUNDLE_MEMBERS, _safe_member_name,
                                                 iter_zip_pdfs, stream_to_tempfile)
from geobingan_sync.source_adapters.dropbox import (DropboxAdapter, FETCH_INTERVAL_DAYS,
                                                    _parse, is_due, mark_fetched)

FOLDER = 'https://www.dropbox.com/scl/fo/3nzvg72e15jsygl7j6rjo/h?dl=0&rlkey=abc'
NOW = datetime(2026, 10, 1, 12, 0)
PDF = b'%PDF-1.5\n' + b'x' * 100


# ---------- 連結比對與 dl=1 正規化 ----------

@pytest.mark.parametrize('url', [
    FOLDER,
    'https://www.dropbox.com/scl/fo/abc123/h?rlkey=x&dl=0',
    'https://dropbox.com/sh/abc123/AAA?dl=0',
])
def test_recognised_folders(url):
    assert find_adapter(url) is not None


@pytest.mark.parametrize('url', [
    'https://www.dropbox.com.evil.test/scl/fo/abc/h?dl=0',   # 後綴偽裝
    'https://www.dropbox.com/s/abc/file.pdf',                # 單檔連結，不是資料夾
    'https://www.dropbox.com/',
    'https://www.dropbox.com:8443/scl/fo/abc/h',             # 指定埠號
    'https://dl.dropboxusercontent.com/scl/fo/abc/h',        # 取檔主機不是入口
    '', None, 123,
])
def test_unrecognised_links(url):
    assert find_adapter(url) is None


def test_dl_flag_is_replaced_not_appended():
    """原連結多半是 dl=0，附加 dl=1 會變成兩個 dl 參數，行為沒保證。"""
    got = _parse(FOLDER)
    assert got.count('dl=') == 1 and 'dl=1' in got
    assert 'rlkey=abc' in got, '不可弄丟授權參數'


def test_dl_flag_added_when_absent():
    assert 'dl=1' in _parse('https://www.dropbox.com/scl/fo/abc/h?rlkey=x')


def test_host_is_normalised_to_www():
    assert _parse('https://dropbox.com/sh/abc/AAA?dl=0').startswith('https://www.dropbox.com/')


# ---------- zip 成員路徑安全（zip slip）----------

@pytest.mark.parametrize('name,expected', [
    ('a.pdf', ('', 'a.pdf')),
    ('sub/a.pdf', ('sub', 'a.pdf')),
    ('./a.pdf', ('', 'a.pdf')),
    ('x/y/a.pdf', ('x/y', 'a.pdf')),
])
def test_safe_member_names(name, expected):
    assert _safe_member_name(name) == expected


@pytest.mark.parametrize('name', [
    '../a.pdf', 'sub/../../a.pdf', '/etc/a.pdf', 'C:/x.pdf',
    'dir/', '', '..',
])
def test_unsafe_member_names_are_rejected(name):
    """路徑由對方控制。path 會變成目標 Drive 的子資料夾名，不能讓它跳出去。"""
    assert _safe_member_name(name) is None


def _zip(members, path=None):
    p = path or tempfile.mkstemp(suffix='.zip')[1]
    with zipfile.ZipFile(p, 'w') as z:
        for name, data in members:
            z.writestr(name, data)
    return p


def test_iter_zip_pdfs_yields_only_pdfs():
    p = _zip([('a.pdf', PDF), ('b.PDF', PDF), ('c.png', b'\x89PNG'), ('d/', b'')])
    try:
        got = [(f, n) for f, n, _d in iter_zip_pdfs(p)]
        assert got == [('', 'a.pdf'), ('', 'b.PDF')]
    finally:
        os.unlink(p)


def test_iter_zip_pdfs_checks_magic_bytes():
    """壓縮檔裡的副檔名同樣是對方說了算。"""
    p = _zip([('real.pdf', PDF), ('fake.pdf', b'<html>not a pdf')])
    try:
        assert [n for _f, n, _d in iter_zip_pdfs(p)] == ['real.pdf']
    finally:
        os.unlink(p)


def test_iter_zip_pdfs_skips_traversal_members():
    p = _zip([('../escape.pdf', PDF), ('ok.pdf', PDF)])
    try:
        assert [n for _f, n, _d in iter_zip_pdfs(p)] == ['ok.pdf']
    finally:
        os.unlink(p)


def test_iter_zip_pdfs_keeps_subfolder_paths():
    p = _zip([('114.07/a.pdf', PDF)])
    try:
        assert [(f, n) for f, n, _d in iter_zip_pdfs(p)] == [('114.07', 'a.pdf')]
    finally:
        os.unlink(p)


def test_too_many_members_is_rejected():
    p = _zip([(f'f{i}.pdf', PDF) for i in range(12)])
    try:
        with pytest.raises(AdapterError, match='too_many_members'):
            list(iter_zip_pdfs(p, max_members=10))
    finally:
        os.unlink(p)


def test_declared_uncompressed_size_is_capped():
    """壓縮炸彈的第一道：看 zip 目錄宣告的解壓後大小，連讀都不用讀。

    成員數仍在上限內，所以只有「宣告量」這道擋得住——注回驗證時單獨拿掉這道就會紅。
    """
    p = _zip([('big.pdf', PDF + b'y' * 5000)])
    try:
        with pytest.raises(AdapterError, match='uncompressed_too_large'):
            list(iter_zip_pdfs(p, max_members=10, max_uncompressed=100))
    finally:
        os.unlink(p)


def test_corrupt_member_is_skipped_not_fatal():
    """單一成員壞掉不該讓整包其他份一起沒了。

    也是「宣告值造假」的實際防線：把 zip 目錄的 file_size 改小，zipfile 會因 CRC
    不符報錯，我們接住、略過該成員、繼續吐出其餘。所以不另寫「實際讀取量」上限
    ——那道在宣告量已過關的前提下永遠觸發不到（注回驗證時發現的）。
    """
    import zipfile
    p = _zip([('bad.pdf', PDF + b'y' * 500), ('good.pdf', PDF)])
    try:
        orig = zipfile.ZipFile.infolist

        def _lying(self):
            out = orig(self)
            for i in out:
                if i.filename == 'bad.pdf':
                    i.file_size = 10          # 目錄謊報，讀取時 CRC 會不符
            return out
        zipfile.ZipFile.infolist = _lying
        try:
            got = [n for _f, n, _d in iter_zip_pdfs(p, max_members=10)]
        finally:
            zipfile.ZipFile.infolist = orig
        assert got == ['good.pdf'], f'壞成員要略過、好成員要留下，實際 {got}'
    finally:
        os.unlink(p)


def test_bad_zip_raises():
    fd, p = tempfile.mkstemp(suffix='.zip')
    os.write(fd, b'not a zip at all')
    os.close(fd)
    try:
        with pytest.raises(AdapterError, match='bad_zip'):
            list(iter_zip_pdfs(p))
    finally:
        os.unlink(p)


# ---------- 取用間隔 ----------

def _state(tmp_path):
    return str(tmp_path / 'bundle.json')


def test_first_fetch_is_always_due(tmp_path):
    assert is_due('k', now=NOW, state_path=_state(tmp_path)) is True


def test_not_due_within_the_interval(tmp_path):
    p = _state(tmp_path)
    mark_fetched('k', 20, now=NOW, state_path=p)
    assert is_due('k', now=NOW + timedelta(days=FETCH_INTERVAL_DAYS - 1), state_path=p) is False


def test_due_again_after_the_interval(tmp_path):
    p = _state(tmp_path)
    mark_fetched('k', 20, now=NOW, state_path=p)
    assert is_due('k', now=NOW + timedelta(days=FETCH_INTERVAL_DAYS), state_path=p) is True


@pytest.mark.parametrize('bad', ['', 'not-a-date', None, 12345])
def test_corrupt_timestamp_falls_open_to_fetching(tmp_path, bad):
    """髒資料不可以讓某一包永遠不更新（同 #100 重驗的紀律）。"""
    p = _state(tmp_path)
    json.dump({'k': {'fetched_at': bad}}, open(p, 'w'))
    assert is_due('k', now=NOW, state_path=p) is True


def test_each_bundle_has_its_own_interval(tmp_path):
    p = _state(tmp_path)
    mark_fetched('a', 1, now=NOW, state_path=p)
    assert is_due('a', now=NOW, state_path=p) is False
    assert is_due('b', now=NOW, state_path=p) is True


# ---------- fetch_all ----------

def _fake_stream(zpath):
    def _s(url, hosts, allowed_host_suffixes=frozenset(), max_bytes=0):
        return zpath
    return _s


def test_fetch_all_yields_pdfs_and_marks_the_timestamp(tmp_path):
    z = _zip([('a.pdf', PDF), ('sub/b.pdf', PDF)], path=str(tmp_path / 'b.zip'))
    p = _state(tmp_path)
    got = list(DropboxAdapter().fetch_all(FOLDER, now=NOW, state_path=p,
                                          stream=_fake_stream(z)))
    assert [(s.name, s.path) for s, _d in got] == [('a.pdf', ''), ('b.pdf', 'sub')]
    assert all(d.startswith(b'%PDF') for _s, d in got)
    assert is_due(_parse(FOLDER), now=NOW, state_path=p) is False, '抓完要記時間戳'


def test_fetch_all_skips_when_not_due(tmp_path, capsys):
    z = _zip([('a.pdf', PDF)], path=str(tmp_path / 'b.zip'))
    p = _state(tmp_path)
    mark_fetched(_parse(FOLDER), 1, now=NOW, state_path=p)

    def _never(*a, **k):
        raise AssertionError('未到間隔不該下載整包')
    got = list(DropboxAdapter().fetch_all(FOLDER, now=NOW, state_path=p, stream=_never))
    assert got == []
    assert '未到取用間隔' in capsys.readouterr().out


def test_tempfile_is_removed_even_when_extraction_fails(tmp_path):
    """暫存檔一定要刪，失敗也一樣——284MB 留在磁碟上很快就會滿。"""
    fd, z = tempfile.mkstemp(suffix='.zip')
    os.write(fd, b'not a zip')
    os.close(fd)
    with pytest.raises(AdapterError, match='bad_zip'):
        list(DropboxAdapter().fetch_all(FOLDER, now=NOW, state_path=_state(tmp_path),
                                        stream=_fake_stream(z)))
    assert not os.path.exists(z), '解壓失敗也要刪掉暫存檔'


def test_tempfile_is_removed_on_success(tmp_path):
    z = _zip([('a.pdf', PDF)], path=str(tmp_path / 'b.zip'))
    list(DropboxAdapter().fetch_all(FOLDER, now=NOW, state_path=_state(tmp_path),
                                    stream=_fake_stream(z)))
    assert not os.path.exists(z)


def test_list_files_refuses_so_nobody_uses_the_wrong_path():
    """整包來源若被當成逐檔來源用，會為每個檔案重複下載整包（實測最大 284MB）。"""
    with pytest.raises(AdapterError, match='use_fetch_all'):
        DropboxAdapter().list_files(FOLDER)


def test_adapter_declares_its_download_scope():
    a = DropboxAdapter()
    assert a.download_host_suffixes == frozenset({'dl.dropboxusercontent.com'})
    assert 'www.dropbox.com' in a.download_hosts


def test_registered_in_adapters():
    assert 'dropbox' in [a.name for a in ADAPTERS]


# ---------- 管線：bundle 必須走 bundle 路徑 ----------

def test_sync_routes_bundle_adapters_to_the_bundle_path(monkeypatch):
    import geobingan_sync.steps.sync_permits as sp
    calls = []

    class _Bundle:
        name = 'bundle'

        def fetch_all(self, url):
            calls.append('fetch_all')
            return iter(())
    ps = sp.PermitSync.__new__(sp.PermitSync)
    ps._print = lambda *a, **k: None
    ps.preload_target_files = lambda *a, **k: None
    ps.save_state = lambda *a, **k: None
    ps.state = {'processed': {}, 'errors': []}
    import threading
    ps._state_lock = threading.Lock()
    ps.copied_total = ps.adapter_uploaded = ps.adapter_failed = ps.permits_with_new = 0
    ps._target_file_cache = {}
    monkeypatch.setattr(sp.PermitSync, '_sync_via_bundle',
                        lambda self, *a, **k: calls.append('bundle_path'))
    ps._sync_via_adapter('111建字第0001號', FOLDER, 'target', _Bundle())
    assert calls == ['bundle_path'], '有 fetch_all 的 adapter 必須走整包路徑'
