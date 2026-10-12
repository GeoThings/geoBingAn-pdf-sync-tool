"""測試不可碰正式 state 檔——這一組盯住那道防線本身。

2026-10-12 的事故（PR #114 合併後，在正式目錄跑整套才發現）：
`test_report_date_upload.py` 呼叫 `upload_pdfs.process_single_pdf()`，成功路徑
立刻 `save_state(state)`，而 `STATE_FILE` 相對 CWD ⇒ 寫到**營運中的 7 天上傳
帳本**。帳本是 read-merge-write 取聯集，所以測試那筆假的「資料夾/檔名」會被
永久併進去，那份 PDF 之後永遠不會上傳，而且完全沒有錯誤訊息。

當天沒出事是運氣：測試用的檔名剛好已經在帳本裡（聯集後集合不變，只有順序變）。
整套 1025 條全綠、帳本已經被改寫——**沒有人在對帳**，這才是真正的缺口。

所以防線有兩層，這一組兩層都要有真正的碰撞：
1. 寫入當場被擋（開檔與 rename 兩個收口）。
2. 整套跑完事後對帳（連 subprocess 這類繞過去的也抓）。
"""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import (PROTECTED_STATE_DIRS, RealStateWriteBlocked,
                            is_protected_path, snapshot_diff, state_snapshot,
                            unguarded_unlink)

from geobingan_sync.steps import upload_pdfs as up


# ───────────────────────── 保護範圍 ─────────────────────────

def test_protected_dirs_cover_both_cwd_and_repo_root():
    """兩個來源都要算：production 的常數一半相對 CWD、一半相對套件位置。"""
    from geobingan_sync import REPO_ROOT
    assert PROTECTED_STATE_DIRS, '保護清單是空的，等於整道防線沒有作用'
    assert (Path(REPO_ROOT) / 'state').resolve() in PROTECTED_STATE_DIRS
    assert Path('state').resolve() in PROTECTED_STATE_DIRS


def test_protection_is_about_the_directory_not_the_filename():
    d = PROTECTED_STATE_DIRS[0]
    assert is_protected_path(d / 'uploaded_to_geobingan_7days.json')
    assert is_protected_path(d / 'weekly_snapshots' / 'x.json'), '子目錄也算'
    assert not is_protected_path(Path('/tmp') / 'uploaded_to_geobingan_7days.json'), (
        '同名但在暫存目錄的檔案不該被擋，否則測試沒地方寫'
    )
    assert not is_protected_path(3), '檔案描述子不是路徑'


# ───────────────────────── 第一層：寫入當場被擋 ─────────────────────────
#
# 這兩支**刻意**觸發防線，所以要自己把 attempts 清掉——否則 fixture 的 teardown
# 斷言會把它們判成違規者（它們正是來驗防線有效的）。

@pytest.fixture
def doomed_target(request):
    """正式 state 目錄裡一個**每次都不同名**的目標檔，離開時一定清掉。

    名字不可固定：防線被拆掉時這個檔會真的被建出來（故障注入時實際發生過），
    固定檔名的話殘留檔會讓後面每一輪的 `not target.exists()` 都紅——那是假的
    失敗來源，會蓋掉真正該紅的那一條。離開時不管有沒有建出來都刪一次。
    """
    target = PROTECTED_STATE_DIRS[0] / f'_test_must_never_exist_{uuid.uuid4().hex}.json'

    # 清理要走 unguarded_unlink：Path.unlink 會進被守住的 os.unlink，於是
    # teardown 時再踩一次防線，把這支測試變成 error（實際踩過一次）。
    request.addfinalizer(lambda: unguarded_unlink(target))
    return target


def test_direct_write_to_the_production_state_dir_is_blocked(doomed_target,
                                                            _no_real_state_writes):
    with pytest.raises(RealStateWriteBlocked, match='_test_must_never_exist'):
        open(doomed_target, 'w')
    assert not doomed_target.exists(), '擋下來就不可以留下檔案'
    _no_real_state_writes.clear()


def test_atomic_replace_into_the_production_dir_is_blocked(tmp_path, doomed_target,
                                                           _no_real_state_writes):
    """`save_state` 是寫 `.tmp.<pid>` 再 `os.replace`——只擋開檔會整條漏掉。"""
    src = tmp_path / 'payload.json'
    src.write_text('{}', encoding='utf-8')
    with pytest.raises(RealStateWriteBlocked):
        os.replace(src, doomed_target)
    assert not doomed_target.exists()
    _no_real_state_writes.clear()


def test_write_outside_the_protected_dirs_still_works(tmp_path):
    """反面對照：防線不可擋到暫存路徑，否則上面那兩條無論怎麼寫都會過。"""
    p = tmp_path / 'state' / 'uploaded_to_geobingan_7days.json'
    p.parent.mkdir()
    p.write_text('{"uploaded_files": []}', encoding='utf-8')
    assert json.loads(p.read_text())['uploaded_files'] == []


# ───────────────────────── 上傳帳本的四個路徑都要導開 ─────────────────────────

@pytest.mark.parametrize('const', ['STATE_FILE', 'STATE_LOCK_FILE',
                                   'HISTORY_FILE', 'PDF_INVENTORY_FILE'])
def test_upload_paths_point_outside_production(const):
    """`STATE_LOCK_FILE` 是重點：它在 import 時就由 STATE_FILE 算好，
    只換 STATE_FILE 的話 flock 仍舊開在正式目錄（實測 .lock 的 mtime 真的被動到）。"""
    assert not is_protected_path(getattr(up, const)), f'{const} 還指著正式目錄'


def test_process_single_pdf_writes_the_tmp_ledger_not_the_production_one(monkeypatch):
    """真正的碰撞：照 10/12 那支測試的呼叫方式跑一次，看帳本落在哪。"""
    monkeypatch.setattr(up, 'upload_to_geobingan',
                        lambda *a, **k: {'id': 'rid', 'parse_status': 'processing'})
    monkeypatch.setattr(up, 'download_pdf', lambda *a, **k: b'%PDF-1.7')
    monkeypatch.setattr(up, 'add_to_history', lambda uid: None)
    pdf = {'id': 'x1', 'name': '不存在的測試檔案_請勿進入正式帳本.pdf',
           'folder_name': '999建字第9999號', 'report_date': '2026-09-30'}
    unique_id = f"{pdf['folder_name']}/{pdf['name']}"

    up.process_single_pdf(None, pdf, {'uploaded_files': [], 'errors': []}, 1, 1)

    written = json.loads(Path(up.STATE_FILE).read_text(encoding='utf-8'))
    assert unique_id in written['uploaded_files'], '帳本該寫，只是不能寫到正式的那一份'

    for d in PROTECTED_STATE_DIRS:
        real = d / 'uploaded_to_geobingan_7days.json'
        if real.exists():
            assert unique_id not in json.loads(real.read_text(encoding='utf-8')
                                               ).get('uploaded_files', []), (
                '假紀錄進了正式帳本——那份 PDF 之後永遠不會被上傳')


# ───────────────────────── 第二層：事後對帳 ─────────────────────────

def test_snapshot_detects_a_new_file(tmp_path):
    before = state_snapshot([tmp_path])
    (tmp_path / 'new.json').write_text('{}', encoding='utf-8')
    assert snapshot_diff(before, state_snapshot([tmp_path])) == [
        str(tmp_path / 'new.json')]


def test_snapshot_detects_a_rewrite_with_the_same_size(tmp_path):
    """同長度的改寫也要抓到——10/12 那次帳本就是長度一樣、只有順序變。"""
    f = tmp_path / 'ledger.json'
    f.write_text('{"a": 1}', encoding='utf-8')
    before = state_snapshot([tmp_path])
    os.utime(f, (0, 0))                     # 只改 mtime，大小不變
    assert snapshot_diff(before, state_snapshot([tmp_path])) == [str(f)]


def test_snapshot_values_are_comparable_without_serialisation(tmp_path):
    """快照值不可經序列化再比：tuple 被 json 讀回來會變 list，於是**每個檔案**
    都比對成「被改過」。我自己量這件事時就先這樣誤判了一次（10 個 vs 真實 2 個）。"""
    (tmp_path / 'a.json').write_text('{}', encoding='utf-8')
    snap = state_snapshot([tmp_path])
    assert all(isinstance(v, tuple) for v in snap.values())
    round_tripped = json.loads(json.dumps({k: list(v) for k, v in snap.items()}))
    assert snapshot_diff(round_tripped, snap), (
        '序列化後的值與原值比對會全部不相等——這就是當初的誤判來源'
    )
    assert snapshot_diff(snap, snap) == [], '同型別比對才會安靜'


def test_protection_does_not_follow_a_chdir(tmp_path, monkeypatch):
    """保護範圍在 conftest 匯入時就定好，不跟著 CWD 漂。

    這一條才是上面那條「兩個來源都要算」真正有力的版本：從 repo 根跑 pytest 時
    `./state` 與 `REPO_ROOT/state` 恰好是同一個目錄，那條斷言會因為巧合而過。
    這裡製造出兩者**不同**的情形：chdir 之後若保護範圍跟著走，正式目錄就會
    在最需要防線的那一刻失去保護。
    """
    original = PROTECTED_STATE_DIRS[0] / 'uploaded_to_geobingan_7days.json'
    (tmp_path / 'state').mkdir()
    monkeypatch.chdir(tmp_path)
    assert is_protected_path(original), 'chdir 之後正式目錄仍必須被保護'
    assert not is_protected_path(tmp_path / 'state' / 'x.json'), (
        '暫存目錄不可因為 chdir 就被當成正式目錄（測試會沒地方寫）')
