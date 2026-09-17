"""文件與程式的一致性守門（PR #85 review 連三輪抓到同一類漂移）。

一、文件裡的指令範例，旗標必須真的存在。

第一輪：文件殘留月模型敘述。第二輪：retry_parse 的 docstring 寫了
`--pending-from`，而 argparse 只有 `--ids-file`，照著做會直接 unknown argument。

文件漂移不會讓測試變紅，只會讓照做的人踩坑，所以用測試釘住：掃過 README、
docs/ 與各 step 模組自己的 docstring，凡是 `python -m <模組> ... --flag` 形式的
範例，flag 都要在該模組的 argparse 裡找得到。用正規表示式讀原始碼取旗標，
不 import 模組（import 會觸發認證載入）。
"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 模組 → 原始碼路徑
CLI_MODULES = {
    'geobingan_sync.steps.retry_parse': 'geobingan_sync/steps/retry_parse.py',
    'geobingan_sync.steps.upload_pdfs': 'geobingan_sync/steps/upload_pdfs.py',
    'geobingan_sync.steps.sync_permits': 'geobingan_sync/steps/sync_permits.py',
    'geobingan_sync.steps.match_permits': 'geobingan_sync/steps/match_permits.py',
    'geobingan_sync.budget': 'geobingan_sync/budget.py',
}

DOCS = ['README.md', 'docs/architecture.md', 'docs/troubleshooting.md', '.env.example']

ALWAYS_VALID = {'--help', '-h'}


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def _declared_flags(src_path):
    """從原始碼抽出 argparse 宣告的旗標（含 add_argument 的長短兩式）。"""
    src = _read(src_path)
    flags = set(re.findall(r"add_argument\(\s*'(--?[A-Za-z0-9][A-Za-z0-9-]*)'", src))
    return flags | ALWAYS_VALID


def _documented_usages():
    """回傳 (來源檔, 行號, 模組, 旗標) 清單。"""
    out = []
    sources = DOCS + list(CLI_MODULES.values())
    for doc in sources:
        for lineno, line in enumerate(_read(doc).split('\n'), 1):
            if '-m ' not in line:
                continue
            for mod in CLI_MODULES:
                if mod not in line:
                    continue
                # 只取模組名之後出現的旗標，避免撈到同一行其他東西
                tail = line.split(mod, 1)[1]
                for flag in re.findall(r'(?<![\w-])(--[a-z0-9][a-z0-9-]*)', tail):
                    out.append((doc, lineno, mod, flag))
    return out


def test_scanner_actually_finds_usages():
    """先確認掃描器抓得到東西，否則這支測試會因為掃不到而假綠。"""
    usages = _documented_usages()
    assert len(usages) >= 5, usages
    assert any(m == 'geobingan_sync.steps.retry_parse' for _, _, m, _ in usages)


def test_documented_flags_exist_in_argparse():
    declared = {mod: _declared_flags(path) for mod, path in CLI_MODULES.items()}
    bad = [f'{doc}:{ln}: `python -m {mod}` 不支援 {flag}'
           for doc, ln, mod, flag in _documented_usages()
           if flag not in declared[mod]]
    assert not bad, '文件示範了不存在的旗標：\n  ' + '\n  '.join(bad)


# ---------- 二、預算改日模型後，描述「現行機制」的月模型詞彙一律不得出現 ----------

# 這些詞只會用來描述**當下的**機制，日模型上線後沒有任何正當用法。
# 刻意不收「月上限」：architecture 需要寫「後端是每日 US$20，**不是月上限**」這種
# 對照歷史的敘述；把歷史說明一起禁掉會逼人改寫正確的文字。
STALE_MONTHLY_TERMS = [
    '跨月',
    'month_rolled_over',
    '月份守門',
    '新月份',
    '本月解析預算帳本',
    'MONTHLY_BUDGET_USD',
]

BUDGET_SURFACE = [
    'geobingan_sync/budget.py',
    'geobingan_sync/steps/upload_pdfs.py',
    'geobingan_sync/steps/retry_parse.py',
    'health_check.py',
    'README.md',
    'docs/architecture.md',
    'docs/troubleshooting.md',
    '.env.example',
]


def test_no_stale_monthly_vocabulary_on_budget_surface():
    """預算相關的程式訊息、docstring、註解與文件都不得再講月模型。

    review 連三輪抓到的都是同一件事：機制已改成日，敘述還停在月。使用者照著
    troubleshooting 找「跨月」找不到，或照著 docstring 用不存在的旗標。程式能跑
    不代表描述是對的，所以用測試釘住。
    """
    bad = []
    for rel in BUDGET_SURFACE:
        for lineno, line in enumerate(_read(rel).split('\n'), 1):
            for term in STALE_MONTHLY_TERMS:
                if term in line:
                    bad.append(f'{rel}:{lineno}: 仍出現「{term}」→ {line.strip()[:70]}')
    assert not bad, '預算改日模型後仍殘留月模型敘述：\n  ' + '\n  '.join(bad)


def test_stale_term_scanner_reads_real_files():
    """防假綠：掃描清單裡的檔案都要真的讀得到且非空。"""
    for rel in BUDGET_SURFACE:
        assert len(_read(rel)) > 200, rel


@pytest.mark.parametrize('mod,expected', [
    ('geobingan_sync.steps.retry_parse', '--ids-file'),
    ('geobingan_sync.steps.upload_pdfs', '--catchup-days'),
])
def test_key_flags_are_declared(mod, expected):
    """釘住幾個關鍵旗標，避免改名後文件與程式一起漂走而測試仍綠。"""
    assert expected in _declared_flags(CLI_MODULES[mod])
