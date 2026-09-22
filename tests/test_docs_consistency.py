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
    'geobingan_sync.steps.drain_stuck': 'geobingan_sync/steps/drain_stuck.py',
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
    '月累計',
    '月額度',
    'MONTHLY_BUDGET_USD',
]

# 上面是逐字黑名單，補不完——「月累計」就是這樣漏掉的（我照著「剛改過什麼」列詞，
# 而不是照概念掃）。下面這條才是真正的規則：**預算語境裡不該有「月」**。
# 判準＝同一行同時出現「月」與某個預算詞。這樣「檔名日期超過 1 個月」（日期窗，
# 非預算）、「月度趨勢」（另一個功能）、「監測月報」（檔名格式）都不會被誤判。
BUDGET_WORDS = ['累計', '額度', '上限', '帳本', '預留', '結算', '消耗']

# 講「舊模型」的歷史敘述是正當的，靠這些標記辨識，不必逐條白名單。
HISTORY_MARKERS = ['舊', '不是', '非', '先前', '原本', '曾']

BUDGET_CODE = [
    'geobingan_sync/budget.py',
    'geobingan_sync/steps/upload_pdfs.py',
    'geobingan_sync/steps/retry_parse.py',
    'health_check.py',
]


def test_budget_context_never_says_month():
    """預算語境（同行同時提到「月」與預算詞）一律視為殘留，除非在講歷史。

    逐字黑名單補不完：`月累計結算失敗` 這句面向操作者的訊息就漏過了上一輪掃描。
    改用概念判準才擋得住整類，而不是擋住我恰好想到的那幾個詞。
    """
    bad = []
    for rel in BUDGET_CODE + DOCS:
        for lineno, line in enumerate(_read(rel).split('\n'), 1):
            if '月' not in line or not any(w in line for w in BUDGET_WORDS):
                continue
            if any(m in line for m in HISTORY_MARKERS):
                continue          # 「不是月上限」「舊月格式檔」這類歷史對照是正當的
            bad.append(f'{rel}:{lineno}: 預算語境出現「月」→ {line.strip()[:72]}')
    assert not bad, '日模型下預算語境不該再有「月」：\n  ' + '\n  '.join(bad)

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


# ---------- launchd 巡檢：每個 job 的「正常結束碼」要與 plist 一一對應 ----------

def test_launchd_job_list_matches_plists():
    """新增 plist 卻忘了加進巡檢 → 那個 job 鎖死不會被發現。"""
    import glob
    import re as _re
    src = _read('health_check.py')
    m = _re.search(r'jobs = \{(.+?)\}\n', src, _re.S)
    assert m, '找不到 jobs 定義'
    in_code = set(_re.findall(r"'([a-z]+)':", m.group(1)))
    on_disk = {os.path.basename(p).rsplit('.', 1)[0].split('.')[-1]
               for p in glob.glob(os.path.join(ROOT, 'launchd', '*.plist'))}
    assert in_code == on_disk, f'巡檢清單 {in_code} 與 launchd/ 的 {on_disk} 不一致'


def test_drainstuck_exit4_is_expected():
    """exit 4＝探測到解析引擎異常、今天不放行，是設計上的正常結果，不可每天誤報。"""
    import re as _re
    src = _read('health_check.py')
    m = _re.search(r"'drainstuck': \{([0-9,\s]+)\}", src)
    assert m and '4' in m.group(1), 'drainstuck 的正常結束碼應含 4'
