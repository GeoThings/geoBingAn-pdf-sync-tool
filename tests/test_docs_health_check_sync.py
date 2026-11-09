"""文件不可以自己抄一份「健檢有幾項」——那份拷貝會漂走，而且已經漂走過。

2026-11-09 盤點：`DEFAULT_CHECKS` 實際 13 項，而文件裡寫死的數字是

  README.md:43   健康檢查 10 項（而且漏列未支援來源、同步錯誤、待送告警）
  README.md:163  健康檢查 10 項
  README.md:392  健康檢查異常（10 項）
  README.md:408  每日健康檢查（8 項）        ← 同一份文件裡的另一個值
  docs/architecture.md:38  10 項巡檢
  README.md:44   pytest, 306 cases          ← 實際 1384
  README.md:63   GitHub Actions，63 個測試

同一個數字抄了五份、而且五份裡有兩個不同的值。這是 #126 那個「30 天檔名窗硬寫
在四處」的同一條教訓：**能從程式碼算出來的事實，文件只放指標，不放拷貝。**
`docs/cron_setup_guide.md` 早就這樣寫了（「項目清單以 `health_check.DEFAULT_CHECKS`
為準，不在這裡抄數量」），README 與 architecture 沒跟上。

所以這裡擋在**文件**這一側：
  1. 現況描述不可以寫死「健檢 N 項」或整套測試的數量。
  2. `DEFAULT_CHECKS` 裡每一項都要在 README 出現，名稱要和程式印出來的一致
     ——否則收到告警的人沒辦法把它對回文件。

歷史紀錄（changelog 裡那種「當時有幾支測試」）不在管制範圍：那是**當時**的事實，
不是現況宣稱。要在現況段落留一個數字就在同一行標〔歷史〕。
"""
import os
import pathlib
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import health_check

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = [ROOT / 'README.md'] + sorted((ROOT / 'docs').glob('*.md'))

#: 現況宣稱裡不可以出現的寫死數量。每一條都真的漂走過（見模組 docstring）。
BANNED = [
    (re.compile(r'健康檢查[^\n]{0,3}?(\d+)\s*項'), '健檢項數'),
    (re.compile(r'(\d+)\s*項巡檢'), '健檢項數'),
    (re.compile(r'pytest,\s*(\d+)\s*cases'), '整套測試數'),
    (re.compile(r'GitHub Actions[，,]\s*(\d+)\s*個測試'), '整套測試數'),
]


def _rel(path):
    try:
        return str(path.relative_to(ROOT))
    except ValueError:           # 自我驗證用的暫存檔不在 repo 裡
        return path.name


def _offending_lines(docs=None):
    out = []
    for path in (docs if docs is not None else DOCS):
        for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if '〔歷史〕' in line:
                continue
            for pattern, what in BANNED:
                m = pattern.search(line)
                if m:
                    out.append(f'{_rel(path)}:{lineno} 寫死了{what}（{m.group(0)}）')
    return out


def test_the_docs_do_not_hardcode_how_many_checks_there_are():
    bad = _offending_lines()
    assert not bad, (
        '文件不要抄數量，改成指向 health_check.DEFAULT_CHECKS（或 pytest 的輸出）：\n  '
        + '\n  '.join(bad))


@pytest.mark.parametrize('name', [n for n, _fn in health_check.DEFAULT_CHECKS])
def test_every_check_is_named_in_the_readme(name):
    """名稱要和程式印出來的一致，收到告警的人才對得回文件。"""
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    assert name in readme, (
        f'DEFAULT_CHECKS 有「{name}」，README 卻沒提到它——'
        '新增檢查項目時要同時寫進 README 的健康檢查清單')


def test_the_guard_itself_catches_a_hardcoded_count(tmp_path):
    """防線要構造出真正的碰撞：塞一行寫死數量進去，上面那支必須紅。

    不這樣驗的話，regex 寫錯（例如全形括號沒涵蓋）會讓這道防線永遠是綠的，
    而它看起來還在上班。
    """
    fake = tmp_path / 'FAKE.md'
    # 這五行是**真的出現過**的五種寫法（見模組 docstring），不是隨手編的樣本：
    # 半角空格、全形括號、倒裝的「N 項巡檢」、pytest cases、CI 個測試。
    # 只放其中一種的話，regex 被改弱成只認那一種也照樣綠——2026-11-09 注入㉓
    # 實測就是這樣漏掉的。
    fake.write_text('- ✅ 健康檢查 10 項：Token／磁碟\n'
                    '- 進入點：每日健康檢查（8 項；異常去重後貼 ClickUp）\n'
                    '| 每日 08:00 | healthcheck | 13 項巡檢：Token／磁碟 |\n'
                    '- ✅ 自動化測試（pytest, 306 cases）\n'
                    '6. ✅ CI 測試保護（GitHub Actions，63 個測試）\n'
                    '- 〔歷史〕健康檢查 8 項（2026-09 當時）\n', encoding='utf-8')
    bad = _offending_lines([fake])
    assert len(bad) == 5, f'五種寫法都要抓到、且不抓〔歷史〕那行，實際：{bad}'
    assert all('FAKE.md' in b for b in bad)
    assert not any('2026-09 當時' in b for b in bad)
