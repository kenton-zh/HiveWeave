"""文本判据棘轮（冻结「靠自然语言子串/正则判意图」的常量清单）。

**背景**：本仓 2026-09-14 全平台排查（`deliverables/text-based-intent-audit-2026-09-14.md`、
`deliverables/text-judgment-escape-inventory-2026-09-14.md`）确认「用文案判断意图」是**结构性**病 ——
换措辞、换语言即绕过（实测 CEO 出口门禁 8 词表，改成「记录之X（不做完工判断）」就放行）。
而它的**复发机制**是习惯：历史上已经在 `tools/bash.py:722-725` 犯过一次
（命令护栏只列 POSIX/DOS 动词 ⇒ 用平台指定的 pwsh 动词写的破坏命令整条绕过），
当时的修法是**往词表里加动词** —— 结构没改，所以必然第三次复发。

> 根因判据（用户 2026-09-14 钦定）：**状态判据**（DB 行 / 系统边界 / 权限位）与语言
> 措辞**无关** ⇒ 可用；**文本判据**（自由文本的子串 / 正则）随措辞与语言**整体失效**
> ⇒ 禁用。本棘轮管的就是后者的**存量冻结与新增拦截**。

**本棘轮问什么、不问什么（重要）**

它问的是 **「表名清单变了吗」**（状态），**不是「你想干什么」**（意图）。
⇒ 它**不判断档位**、不试图识别语义，只在**新增表名**时要求人来审一次。
这与本项目刚立的全局判据同构，也是它唯一站得住的形态。

**两级（分开是为了让报错自己说清是哪一类）**

  1. ``table``  —— 名单型文本表：名字以 ``_NEEDLES/_PATTERNS/_KEYWORDS/_SIGNATURES/
     _PHRASES/_MARKERS/_WORDS/_TOKENS/_TERMS`` 结尾（**大小写不敏感**），
     且右值不是**非字符串标量**。
     **口径实测（本仓全历史 708 提交）**：这类常量历史上共新增过 26 个，
     **26/26 全部是真·文本判据表**（`_COMPLETION_ASSERT_NEEDLES`、
     `RUNNER_FAILURE_SIGNATURES`、`_CLAIM_PATTERNS`、`SELF_DESTRUCTIVE_PATTERNS`…）
     ⇒ 精确率 ≈100%，所以这一级**零容忍**：新名字即红。
  2. ``regex`` —— 模块级 ``re.compile(...)`` 常量。这一类历史上新增约 80 个，
     **多数是纯粹的解析正则**（`_WT_LIST_RE`、`_Z_RECORD_RE`、`_TASK_BRANCH_RE`
     之类，且有 2–3 次是改名重加）⇒ 一并零容忍会天天误报，把人训练成
     「红了就 append」—— 那棘轮就废了。所以它**仍是红**，但报错文案单列。

**⚠ 不要把它读成「禁止一切文本匹配」**：白名单里的 B/C 档（半结构化 token、
机器格式解析）是**允许**的；白名单里的 A 档（自由自然语言）是**待清理债务**。
删表**允许**（只挡新增）—— 清债不需要先改棘轮。

**⚠ 这个棘轮不覆盖什么（必须说清，否则会被误当"文本判据已经管住了"）**

扫描面是**模块级（顶层 ``tree.body``）赋值**（名字大小写不限）。以下形态**不在扫描面内**，
已实测存在真实 A 档实例（41 处，见 `deliverables/text-judgment-escape-inventory-2026-09-14.md`）：

  · **函数/类内局部变量**承载的文本表（`agents/helpers/rate_limit.py:63` 的 `phrases`
    —— 402 全局熔断挂在 4 条英文短语上）；
  · **行内字面量**（`services/code_audit.py:1143` 的 `"[high]" in i.lower()` —— 质量门的
    唯一开关根本没有名字）；
  · **模块级但名字不含后缀**的表（`_LITERALS`/`_LEADS`/`_SENTINELS`/`_ALIASES`/`_HINTS`/
    `_IDS`/`_NAMES`/`_MAP`… 实测 11 处）；
  · 顶层复合语句（`if TYPE_CHECKING:` / `try:`）**内部**的赋值（``tree.body`` 不递归；
    当前仓库该形态存量 A 档为 0，但结构上开着）；
  · 模块级**海象表达式**赋值（`(_FOO_PATTERNS := (…))`，`ast.NamedExpr`，非 Assign）。

⇒ 别把「棘轮绿」当成「文本判据已清完」。棘轮只封**新增**，且只封**它看得见的那一类**。

**已知可绕（接受，并写明为什么不修）**：
  · 换同义后缀名（``_FOO_LITERALS`` / ``_FOO_ALIASES`` / ``_FOO_HINTS``）⇒ 绕得过；
  · ``globals()[f"_{name}_PATTERNS"] = (...)`` 动态构造 ⇒ 绕得过；
  · 把表写进函数体 ⇒ 绕得过（见上）。
**为什么不修**：棘轮的**成功标准是「新增被看见」，不是「新增不可能」**。它防的是
**无意识的复制粘贴惯性**；蓄意绕过需要绕行者先知道棘轮存在，而棘轮的存在本身已提高成本。
对蓄意绕过与上述五类不在扫描面的形态，依赖 code review 与 §六 残留清单。

**五张网各自守什么、不守什么（本仓纪律：说「有测试守着」时必须同时说明它不守什么）**

  | 网 | 守 | 不守 |
  |---|---|---|
  | `test_no_new_text_judgment_tables` | 新增的**后缀型**文本表 | 换名 / 局部变量 / 行内字面量 / 海象 |
  | `test_no_new_module_regex_constants` | 新增的**模块级**正则常量 | 同上 |
  | `test_scanner_sees_every_baselined_entry` | **「文件扫到了、但某条已登记的表静默消失」**（分类逻辑被改坏 / 赋值形状变了） | 整个文件被漏扫（由下一条守）；蓄意 `--write` |
  | `test_scanner_covers_every_baselined_file` | 扫描器被**静默削弱**（路径漂移、rglob pattern 写错、文件解析失败） | 文件扫到了但条目没了（由上一条守） |
  | `test_scanner_suffix_list_is_frozen` + `_min_bounds` + 指纹 | 「尺子」被改（后缀清单 / 下界 / 判据函数源码） | 蓄意改完顺手 `--write` |

**基线维护**：``tests/_text_judge_baseline.json``。只允许**缩**（清债后重新生成，
不要为了变绿而调高）。

    # 清债（没有新增条目）——正常路径
    cd apps/hiveweave-py && uv run python -m tests.test_text_judge_ratchet --write
    # 有意登记「新增」——必须显式说明理由，所以多一个 flag
    cd apps/hiveweave-py && uv run python -m tests.test_text_judge_ratchet --write --allow-grow

⚠ 闸门比的是**新增的 key 集合**（不是总数）：删 3 条再悄悄加 3 条**同样会**被拒。

**纪律**：本测试的判据是 AST 遍历 + 名字清单比对，**不读代码文本内容**、不做子串断言
（本仓钦定：测试里的守卫禁用文本子串断言 ⇒ 必须 AST）。
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import sys
from typing import NamedTuple

import pytest

_SRC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "hiveweave"
_BASELINE_PATH = pathlib.Path(__file__).with_name("_text_judge_baseline.json")
_SRC_TEXT = pathlib.Path(__file__).read_text(encoding="utf-8")

# 「名单型文本表」的名字后缀（口径来源：fixplan-16items-2026-09-14.md §三 P-1）。
# ⚠ 这张清单**本身被冻结**（见 _suffixes 与 test_scanner_suffix_list_is_frozen）：
# 删掉其中一个后缀 = 该后缀今后新增的表**永久逃逸且无人被通知**。要改就改基线，别偷改这里。
_TABLE_SUFFIXES = (
    "_NEEDLES",
    "_PATTERNS",
    "_KEYWORDS",
    "_SIGNATURES",
    "_PHRASES",
    "_MARKERS",
    "_WORDS",
    "_TOKENS",
    "_TERMS",
)

# 扫描器自检下界（见 test_scanner_is_not_vacuous）。取当前存量的保守下界：
# 真实存量 table=24 / regex=106。**注意这不是债棘轮**（清债会让数字往下走，是允许的），
# 它只用来兜「扫描器整体失效」。⚠ 这两个值**也被冻结**（基线 `_min_bounds`），
# 否则把它们改成 0 就等于把自检网变成恒绿 —— 审计实测过这条。
_MIN_TABLES = 15
_MIN_REGEXES = 70

# 指纹覆盖的函数（决定「什么算数」与「遍历什么」的都必须在里面）。
# ⚠ 用 `ast.unparse` 而不是源码原文：源码原文会把**注释与格式**也算进去 ⇒ 加一行注释就假红。
_FINGERPRINT_FUNCS = (
    "_raw_module_names",
    "_module_assign_targets",
    "_is_re_compile",
    "_is_scalar",
    "_tier_of",
    "scan_with_files",
)


class Scan(NamedTuple):
    found: dict[str, dict[str, object]]
    scanned_files: list[str]
    unparsed_files: list[str]
    toplevel_names: dict[str, set[str]]


def _raw_module_names(tree: ast.Module) -> set[str]:
    """顶层**全部**赋值名（**独立于分类器**，不看档位、不筛大小写）。

    ⚠ 必须独立实现、**不许复用 `_module_assign_targets`**：entry 级覆盖网要比对的
    是「源码顶层到底有没有这个名字」。若复用分类路径，则**分类器一坏、两边一起
    缩小**，网就自己失效 —— 写对照组时实测踩到：给分类器加一层名字过滤后，
    覆盖网**照样全绿**（它根本没机会发现自己漏了东西）。
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _module_assign_targets(node: ast.stmt) -> tuple[list[str], ast.AST] | None:
    """模块级（顶层）赋值 ⇒ (名字列表, 右值)；否则 None。

    只认顶层 ``tree.body``：函数内局部变量不构成可复用的判据表。

    ⚠ **不再用 ``name.isupper()`` 预筛**：审计实测 ``_completion_NEEDLES = (…)`` /
    ``gate_re = re.compile(…)`` 这类「名字带小写」的常量会因预筛**整体逃逸**，
    而名字看起来就是一张表，比"无后缀名"更隐蔽。改为**不筛大小写**，
    由 ``_tier_of`` 按后缀（大小写不敏感）/ ``re.compile`` 判定。
    实测代价为 0 条（今天一条都不会多收录）。
    """
    if isinstance(node, ast.Assign):
        value = node.value
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        value = node.value
        names = [node.target.id]
    else:
        return None
    if value is None or not names:
        return None
    return names, value


def _is_re_compile(value: ast.AST) -> bool:
    """右值是否是 ``<anything>.compile(...)``（``re.compile`` / ``re_.compile``）。"""
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "compile"
    )


def _is_scalar(value: ast.AST) -> bool:
    """数与非**文本**字面量。

    ⚠ ``str`` **不算**标量：``_FOO_PATTERNS = "a|b|c"``（单条字符串承载多候选）与
    ``_FOO_NEEDLES = "rm -rf"`` 是货真价实的文本表。第一版把 str 也当标量 ⇒
    这两条**整体逃逸**（既不入基线也不报红），审计已复现。
    ⚠ ``bytes`` 同理（审计第二轮补：``_FOO_PATTERNS = b"a|b"`` 曾逃逸）。
    实测本仓 str/bytes 型存量 0 条，故收紧后基线不变。
    """
    return isinstance(value, ast.Constant) and not isinstance(value.value, (str, bytes))


def _tier_of(name: str, value: ast.AST) -> str | None:
    """分类：``table``（名单型文本表）/ ``regex``（单条正则）/ None（无关常量）。

    ⚠ 判定**次序**：后缀优先于 ``re.compile``。否则
    ``_FOO_PATTERNS = re.compile(r"a|b")``（用单条 alternation 承载多候选词表）
    会落进「登记即可」的 regex 档，等于**换个写法就降档**。审计第二轮实测该路径成立。
    """
    if name.upper().endswith(_TABLE_SUFFIXES) and not _is_scalar(value):
        return "table"
    if _is_re_compile(value):
        return "regex"
    return None


def scan_with_files() -> Scan:
    """扫描全平台源码。

    ``found``      —— ``{"<relpath>::<NAME>": {"tier":…, "line":…}}``
    ``scanned_files`` —— **成功解析**的文件（相对路径，posix）
    ``unparsed_files`` —— 解析失败（SyntaxError / 编码错 / IO 错）的文件
    ``toplevel_names`` —— 每个文件**顶层出现过的全部赋值名**（不分大小写、
        不看档位）。这是 entry 级覆盖网的依据：只靠"文件扫到了"挡不住
        「文件在、也被扫到、但某条已登记的表静默消失」—— 审计第二轮实测：
        把 ``isupper`` 预筛收紧成 ``isupper() and startswith("_")`` 后
        **6 passed 全绿却静默丢失 6 条已登记 table**。
    """
    found: dict[str, dict[str, object]] = {}
    scanned: list[str] = []
    unparsed: list[str] = []
    toplevel: dict[str, set[str]] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
            # ⚠ **不能静默 continue**（第一版就是）：该文件的基线条目会立刻变成
            # 「已删表」（设计上允许）⇒ 棘轮静默少守一片区域。改为向上报告。
            # 审计实测 SyntaxError / UnicodeDecodeError / OSError 三连已够全
            # （NUL 字节、BOM、非法 UTF-8、同名目录四种形态全覆盖）。
            unparsed.append(rel)
            continue
        scanned.append(rel)
        toplevel[rel] = _raw_module_names(tree)
        for node in tree.body:
            parsed = _module_assign_targets(node)
            if parsed is None:
                continue
            names, value = parsed
            for name in names:
                tier = _tier_of(name, value)
                if tier is None:
                    continue
                found[f"{rel}::{name}"] = {"tier": tier, "line": node.lineno}
    return Scan(found, scanned, unparsed, toplevel)


def scan() -> dict[str, dict[str, object]]:
    return scan_with_files().found


def baseline() -> dict:
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def _growth(tier: str) -> dict[str, int]:
    """基线里没有、但现在存在的该档条目（按 relpath::NAME 计）。"""
    known = baseline()["entries"]
    return {
        key: int(meta["line"])
        for key, meta in sorted(scan().items())
        if meta["tier"] == tier and key not in known
    }


_TABLE_HELP = (
    "新增了一张名单型文本表。**先判它属于哪一档**（判据来源二分，见本文件 docstring）：\n"
    "  · 若它匹配的是**自由自然语言**（判「agent/人想干什么」）⇒ 这是 A 档债务，\n"
    "    不要把名字塞进基线。改写成**状态判据**（DB 行 / 系统边界 / 权限位 / 事实位），\n"
    "    或把「意图」落成**显式动作 + 工具内部校验**（验收问句：能否写一个绕过它的调用方？）。\n"
    "  · 若它匹配的是**半结构化 token / 机器格式**（B/C 档，如 `rm -rf`、git porcelain）⇒ 允许。\n"
    "    确认后执行 `uv run python -m tests.test_text_judge_ratchet --write --allow-grow` 登记，\n"
    "    并在提交信息里写明「为什么这条只能是文本判据、它的上位状态判据是什么」。\n"
    "⚠ 不要用「补词表」的方式扩容已有表（本仓已为此复发两次）。\n"
    "⚠ 也别为了绕开这条而改用同义后缀（`_FOO_LITERALS`/`_ALIASES`/`_HINTS`）或改成小写名\n"
    "   —— 换名字不会让判据变成状态判据，只会让它躲开这道棘轮（见 docstring「不覆盖什么」）。"
)

_REGEX_HELP = (
    "新增了模块级 `re.compile` 常量。大多数这类正则是**解析用**（机器格式），登记即可：\n"
    "    cd apps/hiveweave-py && uv run python -m tests.test_text_judge_ratchet --write --allow-grow\n"
    "但以下两种仍是 A 档债务，**别登记、先改判据**：\n"
    "  · 在**自由文本**里找意图词（例如判「是否宣称完工」）；\n"
    "  · 用**单条 alternation 承载多候选词表**（`re.compile(r\"a|b|c\")`）—— 它与 table 档\n"
    "    是同一类东西，只是换了个写法。本仓已有先例（`tools/bash.py:168 _BLOCKING_VERB_RE`、\n"
    "    `services/process_registry.py:125 _SPAWN_BLOCKING_VERB_RE`、`:131 _KILL_VERB_RE`）。\n"
    "⚠ 若名字以后缀结尾（`*_PATTERNS` 等），它会被归进**零容忍**档而不是本档 —— 那是故意的。\n"
    "若只是**改名**一张既有常量：旧 key 消失（删表不报红）+ 新 key 出现（新增报红）⇒\n"
    "本条会红。这是**有意**的（改名也是清单变更），请顺带 `--write` 并说明改名原因。"
)


def test_no_new_text_judgment_tables():
    """零容忍档：新增名单型文本表即红（精确率实测 ≈100%）。"""
    grew = _growth("table")
    assert not grew, f"新增了 {len(grew)} 张名单型文本表：{grew}\n\n{_TABLE_HELP}"


def test_no_new_module_regex_constants():
    """登记档：新增模块级正则即红（多为解析用，登记即可，但必须先看清是哪一类）。"""
    grew = _growth("regex")
    assert not grew, f"新增了 {len(grew)} 个模块级正则常量：{grew}\n\n{_REGEX_HELP}"


def test_scanner_sees_every_baselined_entry():
    """entry 级覆盖网：已登记的条目不许**静默消失**。

    覆盖网的上一版只断言「基线文件 ⊆ 成功解析的文件」，而 ``scanned.append``
    发生在条目抽取**之前** ⇒ 「文件在、也被扫到、但条目全没了」完全在其视野外。
    审计第二轮一手复现：把 ``isupper`` 预筛收紧成 ``isupper() and startswith("_")``
    ⇒ **6 passed 全绿**，却静默丢失 6 条已登记 table（含
    ``tools/bash.py::SELF_DESTRUCTIVE_PATTERNS``、``RUNNER_FAILURE_SIGNATURES``）。

    判据：基线的每条 key，若**该名字在源码顶层仍有赋值**（``toplevel_names`` 里能查到），
    却没被 ``found`` 收录 ⇒ 分类逻辑坏了或赋值形状变了 ⇒ 红。
    名字**已从源码消失**不算红（删表是允许的，见设计）。
    """
    result = scan_with_files()
    lost: list[str] = []
    for key, meta in baseline()["entries"].items():
        rel, name = key.split("::", 1)
        if name not in result.toplevel_names.get(rel, set()):
            continue  # 名字已不在源码顶层 ⇒ 视为已删表（允许）
        if key not in result.found:
            lost.append(f"{key}（基线档位 {meta['tier']}）")
    assert not lost, (
        f"这些已登记的条目**仍在源码顶层有赋值，却没被扫描器收录**（棘轮已静默少守）：{lost[:10]}\n"
        "通常是 `_tier_of`/`_is_scalar`/`_module_assign_targets` 被改坏，或该常量的赋值形状变了。\n"
        "若确实是有意移除这张表，请把该名字一并从源码删掉，或 `--write` 重新生成基线。"
    )


def test_scanner_is_not_vacuous():
    """尺子自检：扫描器坏掉 ⇒ 棘轮静默全绿，比没有守卫更危险。

    对应本仓纪律「『看似有守卫』比『没有守卫』更危险」：一条恒绿的守卫会让人
    以为有安全网。⚠ **这条网很粗**（下界是当前存量的一半左右）：整个子树被漏扫
    也能过。真正挡这个的是 `test_scanner_covers_every_baselined_file` 与
    `test_scanner_sees_every_baselined_entry`，本条只兜「整体失效」。

    ⚠ 下界值本身由基线 `_min_bounds` 冻结（否则改成 0 就把它变成恒绿网）。
    """
    found = scan()
    tables = [k for k, v in found.items() if v["tier"] == "table"]
    regexes = [k for k, v in found.items() if v["tier"] == "regex"]
    assert len(tables) >= _MIN_TABLES, (
        f"扫描到 {len(tables)} 张名单型文本表，低于下界 {_MIN_TABLES} —— "
        "扫描器可能已整体失效（源码路径变了？AST 遍历写错？）。棘轮静默失效比没有棘轮更危险。"
    )
    assert len(regexes) >= _MIN_REGEXES, (
        f"扫描到 {len(regexes)} 个模块级正则常量，低于下界 {_MIN_REGEXES} —— 同上。"
    )


def test_scanner_covers_every_baselined_file():
    """覆盖网：基线冻结过的文件必须**仍然被成功扫描**。

    这一条同时挡三种「静默失去覆盖面」：
      · `_SRC_ROOT` 被收窄 / 路径漂移 ⇒ 基线文件不在 `scanned` 里；
      · `rglob` pattern 写错（如 `*/*.py` 丢掉仓库根的文件）⇒ 同上；
      · 某文件解析失败被吞掉 ⇒ 它在 `unparsed` 里。
    审计实测：单看下界时，47 个文件里漏扫 43 个仍绿、4 个整子树被漏扫也仍绿。
    """
    result = scan_with_files()
    frozen = set(baseline().get("_files") or [])
    assert frozen, "基线缺 `_files`，请用 --write 重新生成。"
    missing = sorted(frozen - set(result.scanned_files))
    gone = [rel for rel in missing if not (_SRC_ROOT / rel).exists()]
    if gone:
        pytest.fail(
            "基线里的文件已不存在（重命名/搬迁后棘轮会静默失效，请重新生成）："
            f"{gone[:10]}"
        )
    if missing:
        pytest.fail(
            f"这些文件在磁盘上但**扫描失败**（棘轮已静默少守这片区域）：{missing[:10]}\n"
            f"解析失败清单：{result.unparsed_files[:10]}"
        )


def test_scanner_ruler_is_frozen():
    """尺子冻结：后缀清单与下界不许被静默削弱。

    审计实测的两个漏洞：
      · 从 `_TABLE_SUFFIXES` 删掉 `_NEEDLES` ⇒ table 24→21，**所有自检仍绿**
        ⇒ 该后缀今后新增的表**永久逃逸且无人被通知**；
      · `_MIN_TABLES`/`_MIN_REGEXES` 归零 ⇒ 自检网变恒绿。
    「棘轮冻结了被测量，却没冻结尺子」—— 这与本棘轮要治的原病（把清单当判据）
    是同构的，所以必须一起冻结。
    """
    data = baseline()
    frozen_suffixes = tuple(data.get("_suffixes") or ())
    assert frozen_suffixes, "基线缺 `_suffixes`，请用 --write 重新生成。"
    assert tuple(_TABLE_SUFFIXES) == frozen_suffixes, (
        "后缀清单被改动了。这会**静默放宽/收窄**棘轮：\n"
        f"  基线：{frozen_suffixes}\n"
        f"  现在：{tuple(_TABLE_SUFFIXES)}\n"
        "若是有意为之，请重新生成基线（`--write`）并在提交信息里写明为什么改口径。"
    )
    frozen_bounds = data.get("_min_bounds") or {}
    assert frozen_bounds == {"table": _MIN_TABLES, "regex": _MIN_REGEXES}, (
        f"扫描器下界被改动了（基线 {frozen_bounds} vs 现在 "
        f"{{'table': {_MIN_TABLES}, 'regex': {_MIN_REGEXES}}}）—— 归零会把它变成恒绿网。\n"
        "确需调整请 `--write` 重新生成并在提交信息里说明。"
    )


def test_baseline_is_self_consistent():
    """基线自身格式自检：条数、两档非空且与 `_by_tier` 相符、`_files`↔`entries` 一致、
    key 形状合法、扫描判据指纹一致。

    ⚠ 已知覆盖面：`_files` ↔ `entries` 的集合相等断言只能挡「凭空给**新文件**预登记」；
    **给一个已登记文件预登记未来表名**仍能蒙过（审计第二轮实测）—— 那一条由
    `test_scanner_sees_every_baselined_entry` 在常量真正落进源码时才转红，
    期间（名字还没写进源码）确实无人守。这是**已知残余**，不假装已覆盖。
    """
    data = baseline()
    entries = data["entries"]
    assert len(entries) == data["_total"], (
        f"基线 `_total`({data['_total']}) 与实际条数({len(entries)}) 不一致 —— 手改 JSON 请同步。"
    )
    by_tier = {tier: sum(1 for m in entries.values() if m["tier"] == tier) for tier in ("table", "regex")}
    assert by_tier == data["_by_tier"], (
        f"基线 `_by_tier`({data['_by_tier']}) 与 entries 实算({by_tier}) 不一致。"
    )
    assert set(data["_by_tier"]) == {"table", "regex"}, f"基线两档都应存在，实际：{data['_by_tier']}"

    files = sorted({key.split("::", 1)[0] for key in entries})
    assert files == sorted(data.get("_files") or []), (
        "基线 `_files` 与 `entries` 的来源文件集合不一致（手改 JSON 后自相矛盾）。\n"
        f"  仅 entries 有：{sorted(set(files) - set(data.get('_files') or []))[:5]}\n"
        f"  仅 _files 有：{sorted(set(data.get('_files') or []) - set(files))[:5]}\n"
        "请用 --write 重新生成。"
    )

    bad = [
        key
        for key in entries
        if "::" not in key
        or not key.split("::", 1)[0].endswith(".py")
        or not key.split("::", 1)[1].isidentifier()
    ]
    assert not bad, f"基线 key 形状非法（应形如 `pkg/mod.py::NAME`）：{bad[:5]}"

    assert (data.get("_scanner_fingerprint") or "") == _scanner_fingerprint(), (
        "扫描判据（" + "/".join(_FINGERPRINT_FUNCS) + "）的 AST 被改动了但基线没重新生成。\n"
        "改口径是允许的，但**必须是显式决定** —— 请 `--write` 并说明改了什么。"
    )


def _scanner_fingerprint() -> str:
    """扫描判据的 sha256（`_FINGERPRINT_FUNCS` 五个函数的 **AST** 反解文本）。

    ⚠ 用 `ast.unparse`（而非源码原文）：源码原文会把注释与格式算进去 ⇒
    加一行注释就假红（审计第二轮实测）。AST 反解对注释/空白/换行免疫。

    ⚠ 只能防「改了却没人知道」，防不了蓄意伪造（改完顺手 `--write` 就一致了）——
    与棘轮整体的定位一致：**让变更被看见，而不是让变更不可能**。
    """
    parts = []
    defined = set()
    for node in ast.parse(_SRC_TEXT).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            if node.name in _FINGERPRINT_FUNCS:
                parts.append(ast.unparse(node))
    missing = [name for name in _FINGERPRINT_FUNCS if name not in defined]
    assert not missing, f"指纹清单里的函数不存在（改名/写错 ⇒ 那一项形同虚设）：{missing}"
    return hashlib.sha256("\n\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _write_baseline(allow_grow: bool) -> None:
    """重新生成基线（清债 / 有意登记新增后调用）。

    闸门比的是**新增的 key 集合**，不是总数：只比总数时「删 3 条再悄悄加 3 条」
    能蒙过（审计第二轮实测 130→130 写出成功）。而「先删几条腾位置」在本仓是
    **被鼓励的常态**（清债不必先改棘轮）⇒ 用总数当闸等于每次清债都给未来攒一次免 flag 登记。
    """
    result = scan_with_files()
    assert not result.unparsed_files, (
        f"有文件解析失败，拒绝生成基线（会让棘轮静默少守）：{result.unparsed_files[:10]}"
    )
    known: dict[str, object] = {}
    if _BASELINE_PATH.exists():
        try:
            known = json.loads(_BASELINE_PATH.read_text(encoding="utf-8")).get("entries") or {}
        except (json.JSONDecodeError, OSError):
            known = {}
    new_keys = sorted(set(result.found) - set(known))
    if new_keys and not allow_grow:
        raise SystemExit(
            f"有 {len(new_keys)} 条**新增**条目（总数 {len(known)} → {len(result.found)}）—— 拒绝生成。\n"
            f"新增示例：{new_keys[:5]}\n"
            "清债（无新增）用 `--write`；有意登记新增请用 `--write --allow-grow`，\n"
            "并在提交信息里写明每一条为什么只能是文本判据。"
        )

    files = sorted({key.split("::", 1)[0] for key in result.found})
    payload = {
        "_comment": (
            "文本判据棘轮冻结清单（2026-09-14）。见 tests/test_text_judge_ratchet.py。"
            "只允许缩减；新增需人判档位后显式登记。table 档 = 名单型文本表（精确率高，零容忍）；"
            "regex 档 = 模块级 re.compile 常量（含解析用正则，口径偏宽）。"
            "⚠ 白名单里的 B/C 档是允许的；A 档（自由自然语言）是待清理债务。"
            "⚠ 本棘轮只覆盖模块级顶层赋值，不覆盖局部变量/行内字面量/复合语句内赋值/海象 —— "
            "见 test_text_judge_ratchet.py docstring「不覆盖什么」。"
        ),
        "_regenerate": "cd apps/hiveweave-py && uv run python -m tests.test_text_judge_ratchet --write",
        "_total": len(result.found),
        "_by_tier": {
            "table": sum(1 for v in result.found.values() if v["tier"] == "table"),
            "regex": sum(1 for v in result.found.values() if v["tier"] == "regex"),
        },
        "_suffixes": list(_TABLE_SUFFIXES),
        "_min_bounds": {"table": _MIN_TABLES, "regex": _MIN_REGEXES},
        "_scanner_fingerprint": _scanner_fingerprint(),
        "_files": files,
        "entries": {k: result.found[k] for k in sorted(result.found)},
    }
    _BASELINE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(
        f"wrote {_BASELINE_PATH} ({len(result.found)} entries, "
        f"scanned {len(result.scanned_files)} files)"
    )


if __name__ == "__main__":
    if "--write" in sys.argv:
        _write_baseline(allow_grow="--allow-grow" in sys.argv)
    else:
        print("usage: python -m tests.test_text_judge_ratchet --write [--allow-grow]")
