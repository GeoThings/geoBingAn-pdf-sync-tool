"""來源資料夾由活轉死：偵測與告警（批次 B4）。"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.steps.match_permits import detect_folder_deaths
import health_check

T0 = datetime(2026, 9, 15, 10, 0)


def test_detects_only_alive_to_404():
    prior = {'A': 'alive', 'B': '404', 'C': 'error', 'D': 'alive'}
    registry = {
        'A': {'gov_pdf_url_status': '404', 'name': '南港段', 'pdf_count': 253},
        'B': {'gov_pdf_url_status': '404'},          # 本來就死 → 不算新失效
        'C': {'gov_pdf_url_status': '404'},          # error→404 可能只是暫時失敗 → 不算
        'D': {'gov_pdf_url_status': 'alive'},        # 仍活著
        'E': {'gov_pdf_url_status': '404'},          # 首次出現就死（無 prior）→ 歷史失效
    }
    deaths = detect_folder_deaths(prior, registry)
    assert [d['permit'] for d in deaths] == ['A']
    assert deaths[0]['pdf_count'] == 253


def _write(path, deaths):
    path.write_text(json.dumps({'deaths': deaths}, ensure_ascii=False), encoding='utf-8')


def test_recent_death_is_error(tmp_path):
    p = tmp_path / 'd.json'
    _write(p, [{'permit': '111建字第0311號', 'name': '南港段', 'pdf_count': 253,
                'detected': (T0 - timedelta(days=2)).strftime('%Y-%m-%d')}])
    level, msg = health_check.check_folder_deaths(path=p, now=T0)
    assert level == 'error' and '111建字第0311號' in msg and '253' in msg


def test_old_death_does_not_drive_light(tmp_path):
    p = tmp_path / 'd.json'
    _write(p, [{'permit': 'X', 'pdf_count': 5,
                'detected': (T0 - timedelta(days=30)).strftime('%Y-%m-%d')}])
    level, msg = health_check.check_folder_deaths(path=p, now=T0)
    assert level == 'ok' and '歷史累計 1 個' in msg


def test_missing_file_is_ok(tmp_path):
    assert health_check.check_folder_deaths(path=tmp_path / 'nope.json', now=T0)[0] == 'ok'


def test_corrupt_file_is_ok(tmp_path):
    p = tmp_path / 'd.json'
    p.write_text('{not json', encoding='utf-8')
    assert health_check.check_folder_deaths(path=p, now=T0)[0] == 'ok'
