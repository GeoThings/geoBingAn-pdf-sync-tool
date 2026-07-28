"""Tests for PDF inventory 跨模組資料鏈（PR #69 review P2）。

背景：state['cache']['pdfs'] 對 upload_pdfs 是死快取，但 weekly_snapshot
月度趨勢告警與 analyze_decline 以它為正式資料來源——移除快取時必須改供
獨立的 pdf_inventory.json，且兩個 consumer 都要能讀到（含 legacy fallback）。
此處直接驗「producer 寫 → 兩個 consumer 讀」的跨模組 contract。
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import upload_pdfs
import weekly_snapshot
import analyze_decline

PDFS = [
    {'id': 'p1', 'name': '建案A_1150301.pdf', 'folder_name': '110建字第0001號'},
    {'id': 'p2', 'name': 'B-2026-04-15.pdf', 'folder_name': '110建字第0002號'},
]


def test_producer_to_consumers_chain(tmp_path, monkeypatch):
    """upload_pdfs 寫 inventory → weekly_snapshot / analyze_decline 都讀得到。"""
    inv = tmp_path / 'pdf_inventory.json'
    monkeypatch.setattr(upload_pdfs, 'PDF_INVENTORY_FILE', str(inv))
    monkeypatch.setattr(weekly_snapshot, 'PDF_INVENTORY_FILE', str(inv))
    monkeypatch.setattr(weekly_snapshot, 'DRIVE_CACHE_FILE', str(tmp_path / 'none.json'))
    monkeypatch.setattr(analyze_decline, 'PDF_INVENTORY_FILE', Path(inv))
    monkeypatch.setattr(analyze_decline, 'DRIVE_CACHE_FILE', tmp_path / 'none.json')

    upload_pdfs.save_pdf_inventory(PDFS)

    data = json.loads(inv.read_text(encoding='utf-8'))
    assert data['last_scan']
    assert [p['id'] for p in data['pdfs']] == ['p1', 'p2']

    assert weekly_snapshot._load_pdf_inventory() == PDFS
    assert analyze_decline.load_pdfs() == PDFS


def test_consumers_fall_back_to_legacy_cache(tmp_path, monkeypatch):
    """inventory 尚未產生（升級後第一次 weekly sync 前）→ 讀 legacy state cache。"""
    legacy = tmp_path / 'uploaded.json'
    legacy.write_text(json.dumps({'cache': {'pdfs': PDFS}}), encoding='utf-8')

    monkeypatch.setattr(weekly_snapshot, 'PDF_INVENTORY_FILE', str(tmp_path / 'no-inv.json'))
    monkeypatch.setattr(weekly_snapshot, 'DRIVE_CACHE_FILE', str(legacy))
    monkeypatch.setattr(analyze_decline, 'PDF_INVENTORY_FILE', tmp_path / 'no-inv.json')
    monkeypatch.setattr(analyze_decline, 'DRIVE_CACHE_FILE', legacy)

    assert weekly_snapshot._load_pdf_inventory() == PDFS
    assert analyze_decline.load_pdfs() == PDFS


def test_inventory_preferred_over_legacy(tmp_path, monkeypatch):
    inv = tmp_path / 'pdf_inventory.json'
    inv.write_text(json.dumps({'last_scan': 'x', 'pdfs': PDFS}), encoding='utf-8')
    legacy = tmp_path / 'uploaded.json'
    legacy.write_text(json.dumps({'cache': {'pdfs': [{'id': 'stale'}]}}), encoding='utf-8')

    monkeypatch.setattr(weekly_snapshot, 'PDF_INVENTORY_FILE', str(inv))
    monkeypatch.setattr(weekly_snapshot, 'DRIVE_CACHE_FILE', str(legacy))
    monkeypatch.setattr(analyze_decline, 'PDF_INVENTORY_FILE', Path(inv))
    monkeypatch.setattr(analyze_decline, 'DRIVE_CACHE_FILE', legacy)

    assert weekly_snapshot._load_pdf_inventory() == PDFS
    assert analyze_decline.load_pdfs() == PDFS


def test_load_state_keeps_legacy_cache_until_inventory_exists(tmp_path, monkeypatch):
    """升級保護（review P2）：inventory 尚未成功建立前，load_state 不得
    移除 legacy cache——否則 .pause_upload 期間兩個資料來源同時空掉。"""
    state_file = tmp_path / 'state.json'
    state_file.write_text(json.dumps({
        'uploaded_files': [], 'errors': [],
        'cache': {'pdfs': PDFS, 'folders': [], 'last_scan': None},
    }), encoding='utf-8')
    monkeypatch.setattr(upload_pdfs, 'STATE_FILE', str(state_file))
    monkeypatch.setattr(upload_pdfs, 'HISTORY_FILE', str(tmp_path / 'no-history.json'))
    monkeypatch.setattr(upload_pdfs, 'PDF_INVENTORY_FILE', str(tmp_path / 'no-inv.json'))

    state = upload_pdfs.load_state()
    assert state['cache']['pdfs'] == PDFS  # inventory 不存在 → fallback 保留

    # inventory 建立後 → legacy cache 才可移除
    (tmp_path / 'inv.json').write_text('{"pdfs": []}', encoding='utf-8')
    monkeypatch.setattr(upload_pdfs, 'PDF_INVENTORY_FILE', str(tmp_path / 'inv.json'))
    state = upload_pdfs.load_state()
    assert 'cache' not in state


def test_save_pdf_inventory_reports_failure(tmp_path, monkeypatch):
    """inventory 寫入失敗必須回報 False（且不炸上傳流程）。"""
    monkeypatch.setattr(upload_pdfs, 'PDF_INVENTORY_FILE', str(tmp_path / 'inv.json'))
    assert upload_pdfs.save_pdf_inventory(PDFS) is True

    def _boom(src, dst):
        raise OSError('disk full')
    monkeypatch.setattr(upload_pdfs.os, 'replace', _boom)
    monkeypatch.setattr(upload_pdfs, 'PDF_INVENTORY_FILE', str(tmp_path / 'inv2.json'))
    assert upload_pdfs.save_pdf_inventory(PDFS) is False


def test_both_sources_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_snapshot, 'PDF_INVENTORY_FILE', str(tmp_path / 'a.json'))
    monkeypatch.setattr(weekly_snapshot, 'DRIVE_CACHE_FILE', str(tmp_path / 'b.json'))
    monkeypatch.setattr(analyze_decline, 'PDF_INVENTORY_FILE', tmp_path / 'a.json')
    monkeypatch.setattr(analyze_decline, 'DRIVE_CACHE_FILE', tmp_path / 'b.json')

    assert weekly_snapshot._load_pdf_inventory() == []
    assert analyze_decline.load_pdfs() is None
