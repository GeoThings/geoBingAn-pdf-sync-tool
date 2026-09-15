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
        mp.append_folder_deaths([{'permit': 'A', 'source_url': 'u'}], deaths_file=str(deaths_file))
        assert False, '應該要 raise'
    except ValueError as e:
        assert '損毀' in str(e)
    assert deaths_file.read_text(encoding='utf-8') == '{broken'    # 原檔未被覆寫


def test_health_check_corrupt_file_is_warning_not_green(tmp_path):
    p = tmp_path / 'd.json'
    p.write_text('{not json', encoding='utf-8')
    level, msg = health_check.check_folder_deaths(path=p, now=T0)
    assert level == 'warning' and '損毀' in msg


# ---------- review P2：death 已寫、registry 失敗時重跑不可重複累積 ----------

def test_rerun_after_registry_failure_does_not_duplicate(tmp_path, monkeypatch):
    import geobingan_sync.steps.match_permits as mp
    reg_file = tmp_path / 'registry.json'
    deaths_file = tmp_path / 'deaths.json'
    prior = {'A': 'alive'}
    registry = {'A': {'gov_pdf_url_status': '404', 'name': '南港段', 'pdf_count': 253,
                      'source_url': 'https://drive.google.com/drive/folders/XYZ'}}

    # 第一輪：death 寫入成功，但 registry 提交失敗
    real_write = mp._atomic_write_json
    def fail_on_registry(path, data, label):
        if label == 'registry':
            raise OSError('disk full')
        return real_write(path, data, label)
    monkeypatch.setattr(mp, '_atomic_write_json', fail_on_registry)
    try:
        mp.commit_registry_with_deaths(registry, prior, registry_file=str(reg_file),
                                       deaths_file=str(deaths_file), now=T0)
        assert False, '應該要 raise'
    except OSError:
        pass
    assert len(json.loads(deaths_file.read_text(encoding='utf-8'))['deaths']) == 1
    assert not reg_file.exists()

    # 第二輪＝隔天的正常排程（prior 仍是 alive，因為 registry 沒提交）：不可重複累積
    monkeypatch.setattr(mp, '_atomic_write_json', real_write)
    appended = mp.commit_registry_with_deaths(registry, prior, registry_file=str(reg_file),
                                              deaths_file=str(deaths_file),
                                              now=T0 + timedelta(days=1))
    assert appended == []                                    # 已去重，本輪無新增
    assert len(json.loads(deaths_file.read_text(encoding='utf-8'))['deaths']) == 1
    assert reg_file.exists()                                 # 這次 registry 成功提交


def test_same_folder_next_day_is_not_a_new_event(tmp_path):
    """排程每日執行，重試必在隔天——去重鍵不含日期才擋得住（review P2）。"""
    import geobingan_sync.steps.match_permits as mp
    deaths_file = tmp_path / 'deaths.json'
    d = {'permit': 'A', 'source_url': 'https://drive.google.com/drive/folders/XYZ', 'pdf_count': 1}
    assert len(mp.append_folder_deaths([dict(d)], deaths_file=str(deaths_file), now=T0)) == 1
    assert len(mp.append_folder_deaths([dict(d)], deaths_file=str(deaths_file), now=T0)) == 0
    for day in (1, 2, 30):
        assert len(mp.append_folder_deaths([dict(d)], deaths_file=str(deaths_file),
                                           now=T0 + timedelta(days=day))) == 0
    log = json.loads(deaths_file.read_text(encoding='utf-8'))['deaths']
    assert len(log) == 1 and log[0]['detected'] == '2026-09-15'   # 保留首次偵測日


def test_same_permit_different_folder_is_a_new_event(tmp_path):
    """換了新資料夾又失效＝真的第二次事件，不可被去重掉。"""
    import geobingan_sync.steps.match_permits as mp
    deaths_file = tmp_path / 'deaths.json'
    old = {'permit': 'A', 'source_url': 'https://drive.google.com/drive/folders/OLD', 'pdf_count': 1}
    new = {'permit': 'A', 'source_url': 'https://drive.google.com/drive/folders/NEW', 'pdf_count': 9}
    assert len(mp.append_folder_deaths([dict(old)], deaths_file=str(deaths_file), now=T0)) == 1
    assert len(mp.append_folder_deaths([dict(new)], deaths_file=str(deaths_file),
                                       now=T0 + timedelta(days=60))) == 1
    assert len(json.loads(deaths_file.read_text(encoding='utf-8'))['deaths']) == 2


def test_health_check_counts_deduped_events_only(tmp_path):
    import geobingan_sync.steps.match_permits as mp
    deaths_file = tmp_path / 'deaths.json'
    d = {'permit': 'A', 'source_url': 'u', 'pdf_count': 253}
    for _ in range(3):
        mp.append_folder_deaths([dict(d)], deaths_file=str(deaths_file), now=T0)
    level, msg = health_check.check_folder_deaths(path=deaths_file, now=T0)
    assert level == 'error' and '1 個來源資料夾' in msg and '253' in msg   # 不會誇大成 3 個/759 份
