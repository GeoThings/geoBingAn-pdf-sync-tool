"""process_single_pdf 失敗分類（預算結算用）：download_failed / rejected / unknown。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import geobingan_sync.steps.upload_pdfs as up

PDF = {'id': 'f1', 'name': 'a.pdf', 'folder_name': '111建字第0001號'}


def _setup(monkeypatch, download, upload):
    monkeypatch.setattr(up, 'download_pdf', lambda service, fid, name: download)
    monkeypatch.setattr(up, 'upload_to_geobingan', lambda content, name, folder: upload)
    monkeypatch.setattr(up, 'save_state', lambda state: None)
    monkeypatch.setattr(up, 'add_to_history', lambda uid: None)


def test_download_failure_is_download_failed(monkeypatch):
    _setup(monkeypatch, download=None, upload=None)
    r = up.process_single_pdf(None, PDF, {'uploaded_files': [], 'errors': []}, 1, 1)
    assert r['success'] is False and r['error'] == 'download_failed'


def test_explicit_rejection_is_rejected(monkeypatch):
    _setup(monkeypatch, download=b'%PDF', upload=False)
    r = up.process_single_pdf(None, PDF, {'uploaded_files': [], 'errors': []}, 1, 1)
    assert r['success'] is False and r['error'] == 'rejected'


def test_unknown_exception_is_unknown(monkeypatch):
    _setup(monkeypatch, download=b'%PDF', upload=None)
    r = up.process_single_pdf(None, PDF, {'uploaded_files': [], 'errors': []}, 1, 1)
    assert r['success'] is False and r['error'] == 'unknown'


def test_success_and_processing_count_as_success(monkeypatch):
    state = {'uploaded_files': [], 'errors': []}
    _setup(monkeypatch, download=b'%PDF', upload={'status': 'processing'})   # 逾時/502 視為可能已送達
    r = up.process_single_pdf(None, PDF, state, 1, 1)
    assert r['success'] is True and state['uploaded_files'] == ['111建字第0001號/a.pdf']
