"""上傳必須送出報告日期，否則後端存的是「我們上傳那天」。

2026-10-01 查出：後端的 construction-reports 上傳 API **接受** report_date（說明寫
「報告日期（可選，會從文件中解析）」），但只有 JSON 上傳會從內容抽日期；PDF 沒給就
`report_date = timezone.now().date()`。而我方 payload 從來只送 group_id／report_type／
primary_language，所以每一份 PDF 的 report_date 都是上傳日。

實測比對：今天上傳的 `112建0079-初值報告20260930.pdf` → report_date 2026-10-01；
9/23 送的 15 筆全部 2026-09-23。最近 12 筆中能從檔名推出日期的 4 筆，4 筆全不符。

影響不是顯示瑕疵：report_date 有索引、是預設排序依據、也餵給 latest_report_date
之類的聚合，於是「某工地最近一份報告是什麼時候」實際量到的是「我們最近一次上傳
是什麼時候」——那正是「哪些工地沒有近期資料」這個核心指標。

日期我方早就有（parse_date_from_filename，PR #94 還強化過誤判），只是沒送。
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geobingan_sync.steps import upload_pdfs as up
from geobingan_sync.steps.upload_pdfs import select_pdfs_to_upload

CUTOFF = datetime(2026, 9, 1)


def _pdf(name, folder='112建字第0079號', mtime='2026-10-01T02:00:37.030Z'):
    return {'id': 'x' + name, 'name': name, 'folder_name': folder, 'modifiedTime': mtime}


# ---------- 挑選階段要把日期附上 ----------

def test_selected_pdf_carries_report_date():
    picked, _counts = select_pdfs_to_upload(
        [_pdf('112建0079-初值報告20260930.pdf')], uploaded_files=[], cutoff=CUTOFF)
    assert len(picked) == 1
    assert picked[0]['report_date'] == '2026-09-30', '要用檔名日期，不是上傳日'


@pytest.mark.parametrize('name,expected', [
    ('112建0079-初值報告20260930.pdf', '2026-09-30'),
    ('恆合-錦西街 日報表 115.09.16.pdf', '2026-09-16'),
    ('君隆監測報告115.09.16上傳.pdf', '2026-09-16'),
    ('115.09.17北側基地集合住宅新建工程工地監測報告.pdf', '2026-09-17'),
])
def test_民國_and_西元_filenames_both_yield_report_date(name, expected):
    """這四個檔名形態就是實測比對中 report_date 不符的那幾筆。"""
    picked, _ = select_pdfs_to_upload([_pdf(name)], uploaded_files=[], cutoff=CUTOFF)
    assert picked and picked[0]['report_date'] == expected


def test_undated_pdf_is_skipped_so_every_picked_one_has_a_date():
    """挑選階段就排除解析不出日期的，所以上傳路徑上每一份都有日期。"""
    picked, counts = select_pdfs_to_upload(
        [_pdf('沒有日期的報告.pdf'), _pdf('112建0079-初值報告20260930.pdf')],
        uploaded_files=[], cutoff=CUTOFF)
    assert counts['no_date'] == 1
    assert all(p.get('report_date') for p in picked)


# ---------- payload 要真的帶上去 ----------

class _Resp:
    status_code = 201

    def json(self):
        return {'id': 'rid-1', 'parse_status': 'processing'}


def _capture(monkeypatch):
    """攔住 POST，回傳實際送出的 data。"""
    sent = {}

    def _post(url, files=None, data=None, headers=None, timeout=None):
        sent.update(data or {})
        return _Resp()
    monkeypatch.setattr(up.requests, 'post', _post)
    monkeypatch.setattr(up, '_get_valid_token', lambda: 'tok')
    return sent


def test_payload_includes_report_date(monkeypatch):
    sent = _capture(monkeypatch)
    up.upload_to_geobingan(b'%PDF-1.7', 'a.pdf', '112建字第0079號', report_date='2026-09-30')
    assert sent.get('report_date') == '2026-09-30'


def test_payload_omits_report_date_when_absent_and_warns(monkeypatch, capsys):
    """缺日期不可靜默：後端會退回上傳日，而且從結果看不出來。"""
    sent = _capture(monkeypatch)
    up.upload_to_geobingan(b'%PDF-1.7', 'a.pdf', '112建字第0079號')
    assert 'report_date' not in sent, '沒有日期就不要送空值'
    assert '沒有報告日期可送' in capsys.readouterr().out


def test_existing_payload_fields_are_untouched(monkeypatch):
    sent = _capture(monkeypatch)
    up.upload_to_geobingan(b'%PDF-1.7', 'a.pdf', '112建字第0079號', report_date='2026-09-30')
    for k in ('group_id', 'report_type', 'primary_language'):
        assert k in sent, f'不可弄丟既有欄位 {k}'


def test_process_single_pdf_passes_the_date_through(monkeypatch):
    """守門：process_single_pdf 不可漏傳，否則挑選階段算的日期白算。"""
    seen = {}

    def _upload(content, name, folder, max_retries=3, report_date=None):
        seen['report_date'] = report_date
        return {'id': 'rid', 'parse_status': 'processing'}
    monkeypatch.setattr(up, 'upload_to_geobingan', _upload)
    monkeypatch.setattr(up, 'download_pdf', lambda *a, **k: b'%PDF-1.7')
    monkeypatch.setattr(up, 'flush_state', lambda *a, **k: None)
    pdf = _pdf('112建0079-初值報告20260930.pdf')
    pdf['report_date'] = '2026-09-30'
    up.process_single_pdf(None, pdf, {'uploaded_files': [], 'errors': []}, 1, 1)
    assert seen['report_date'] == '2026-09-30'


def test_upload_call_site_sends_report_date():
    """掃程式碼：呼叫點若漏掉 report_date=，前面做的全部白費。只掃程式碼行。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'geobingan_sync/steps/upload_pdfs.py')
    lines = [ln for ln in open(path, encoding='utf-8')
             if not ln.lstrip().startswith('#')]
    src = ''.join(lines)
    call = src[src.index('result = upload_to_geobingan('):]
    call = call[:call.index(')') + 1]
    assert 'report_date=' in call, f'呼叫點沒帶 report_date: {call}'


# ---------- review P1：月粒度的合成月底不可當成報告日期送出 ----------
#
# parser 對「115年10月」這種只有年月的檔名會**合成**月底（_month_end）。那對 cutoff
# 比對與排序是合理近似，但當成報告日期送出去，當月月報在月初就變成未來日期：
# 2026-10-01 上傳「監測月報115年10月.pdf」會送 2026-10-31，未來 30 天。
# 那會把 latest_report_date 推到未來，比原本的「上傳日」更糟——正好污染本功能
# 要修的那個指標。

from geobingan_sync.filename_date_parser import (parse_date_with_granularity,
                                                 report_date_for_upload)

OCT1 = datetime(2026, 10, 1)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return OCT1


@pytest.mark.parametrize('name', ['監測月報115年10月.pdf', '監測月報202610.pdf',
                                  '115年10月工地監測月報.pdf'])
def test_current_month_report_is_not_sent_as_a_future_date(name):
    got = parse_date_with_granularity(name, now=OCT1)
    assert got is not None and got[1] == 'month'
    assert got[0] == datetime(2026, 10, 31), '前提：parser 合成的是月底'
    assert report_date_for_upload(name, now=OCT1) == '2026-10-01', '不可送未來的月底'


def test_past_month_report_still_sends_month_end():
    """過去的月份沒有未來問題，月底是「涵蓋到哪天」最好的單一日期近似。"""
    assert report_date_for_upload('114年04月月報.pdf', now=OCT1) == '2025-04-30'


@pytest.mark.parametrize('today,expected', [
    (datetime(2026, 10, 1), '2026-10-01'),
    (datetime(2026, 10, 15), '2026-10-15'),
    (datetime(2026, 10, 31), '2026-10-31'),
    (datetime(2026, 11, 5), '2026-10-31'),      # 月份過完就送月底
])
def test_current_month_is_clamped_to_today(today, expected):
    assert report_date_for_upload('監測月報115年10月.pdf', now=today) == expected


def test_day_granularity_is_unchanged():
    assert report_date_for_upload('112建0079-初值報告20260930.pdf', now=OCT1) == '2026-09-30'
    assert report_date_for_upload('宏林TOP31-基地監測報告-1150930.pdf', now=OCT1) == '2026-09-30'


def test_no_future_date_is_ever_sent():
    """通則：任何粒度都不送未來日期。日粒度的合理性檢查容許 ±2 天時區誤差，
    那對 cutoff 是對的，但不該寫進資料。"""
    tomorrow = (OCT1 + timedelta(days=1)).strftime('%Y%m%d')
    name = f'某工地報告{tomorrow}.pdf'
    got = parse_date_with_granularity(name, now=OCT1)
    assert got is not None and got[0] > OCT1, '前提：容許誤差讓它通過合理性檢查'
    assert report_date_for_upload(name, now=OCT1) == '2026-10-01', '仍不可送未來'


def test_unparseable_name_sends_nothing():
    assert report_date_for_upload('沒有日期.pdf', now=OCT1) is None


def test_selection_uses_the_clamped_date_not_the_synthetic_month_end(monkeypatch):
    """管線層：挑選階段附上的必須是夾過的日期，不是 cutoff 用的那個月底。"""
    import geobingan_sync.filename_date_parser as fdp
    monkeypatch.setattr(fdp, 'datetime', _FrozenDatetime)
    picked, _ = select_pdfs_to_upload([_pdf('監測月報115年10月.pdf')],
                                      uploaded_files=[], cutoff=datetime(2026, 9, 1))
    assert picked, '當月月報仍要能被選上（cutoff 比對用月底，這點不可退化）'
    assert picked[0]['report_date'] == '2026-10-01', f"實際 {picked[0]['report_date']}"

