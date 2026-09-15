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




# ---------- review P1：先寫 death、成功才提交 registry ----------

def test_registry_not_committed_when_death_persist_fails(tmp_path, monkeypatch):
    """死亡事件寫入失敗時不可提交 registry，否則下輪 prior 已是 404、永遠偵測不到。"""
    import geobingan_sync.steps.match_permits as mp
    reg_file = tmp_path / 'registry.json'
    deaths_file = tmp_path / 'deaths.json'
    prior = {'A': 'alive'}
    registry = {'A': {'gov_pdf_url_status': '404', 'name': '南港段', 'pdf_count': 253}}

    def boom(*a, **k):
        raise OSError('disk full')
    monkeypatch.setattr(mp, 'append_folder_deaths', boom)

    try:
        mp.commit_registry_with_deaths(registry, prior, registry_file=str(reg_file),
                                       deaths_file=str(deaths_file))
        assert False, '應該要 raise'
    except OSError:
        pass
    assert not reg_file.exists()          # registry 未提交 → 下輪 prior 仍是 alive，可重新偵測


def test_commit_writes_death_then_registry(tmp_path):
    import geobingan_sync.steps.match_permits as mp
    reg_file = tmp_path / 'registry.json'
    deaths_file = tmp_path / 'deaths.json'
    registry = {'A': {'gov_pdf_url_status': '404', 'name': '南港段', 'pdf_count': 253}}
    deaths = mp.commit_registry_with_deaths(registry, {'A': 'alive'},
                                            registry_file=str(reg_file),
                                            deaths_file=str(deaths_file), now=T0)
    assert [d['permit'] for d in deaths] == ['A']
    assert json.loads(deaths_file.read_text(encoding='utf-8'))['deaths'][0]['detected'] == '2026-09-15'
    assert json.loads(reg_file.read_text(encoding='utf-8'))['A']['pdf_count'] == 253
    assert not list(tmp_path.glob('*.tmp'))        # 原子寫入，無殘留暫存檔


def test_corrupt_deaths_file_raises_instead_of_wiping_history(tmp_path):
    """損毀時 raise，不可靜默重置成空陣列而丟掉歷史紀錄。"""
    import geobingan_sync.steps.match_permits as mp
    deaths_file = tmp_path / 'deaths.json'
    deaths_file.write_text('{broken', encoding='utf-8')
    try:
        mp.append_folder_deaths([{'permit': 'A'}], deaths_file=str(deaths_file))
        assert False, '應該要 raise'
    except ValueError as e:
        assert '損毀' in str(e)
    assert deaths_file.read_text(encoding='utf-8') == '{broken'    # 原檔未被覆寫


def test_health_check_corrupt_file_is_warning_not_green(tmp_path):
    p = tmp_path / 'd.json'
    p.write_text('{not json', encoding='utf-8')
    level, msg = health_check.check_folder_deaths(path=p, now=T0)
    assert level == 'warning' and '損毀' in msg
