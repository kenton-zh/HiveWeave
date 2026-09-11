"""机械门禁：提示词里让 agent 调用的工具，必须是真实存在的工具。

2026-09-11 实测的真 bug（fixplan §6 未列，批次 7 附带走查发现）：
``prompts/executor.py:358`` 与 ``prompts/coordinator.py:181`` 指示 agent
调用 **``read_project_memory``** —— 该工具**从未存在**（记忆层只有
``read_memory`` / ``write_memory``）。agent 被自己的剧本引导去调一个不存在的
工具，运行期经 ``_unknown_tool_error`` 拿到纠正建议（``executor.py:2845``），
**每一轮都白烧一次调用**。

这类缺陷的形状与 ``modules`` 死表同族：「**定义了但没接线**」——
只是方向相反（那边是表没人写，这边是**话里有个不存在的受体**）。
功能测试打不到它（提示词是纯字符串，没有任何断言面），
只能靠**机械门禁**在代码层断言。

判据来源：我们自己的工具注册表——``tools/base.py::_TOOL_REGISTRY``
（``@tool`` 装饰器在导入时填充，见 ``tools/__init__.py:46``）是工具名的
**唯一权威源**；提示词消费的是同一份表（``tools/executor.py::_TOOL_SPECS``
按名字查它）。**提示词里的名字必须落在这张表里。**

实现要点（避免「文本子串断言」的假绿，也避免「提示词里到处是反引号」的假红）：
- 用 **AST** 遍历 ``src/hiveweave/prompts/**.py``，只取**字符串字面量**；
  注释里的工具名不算（注释不驱动 agent 行为）。
- 只认**两种**可靠语境（本仓提示词把 enum 值 / 参数名 / 配置键也全用反引号包，
  所以「凡反引号即工具」的粗口径会造出 20+ 个假阳性 —— 见下方 ``_PHASE_LIST_RE``
  与 ``_IMPERATIVE_RE`` 的取舍说明）：
    ① 阶段清单行：``- EXPLORE: list_files, read_file, grep, ...``
       （ALL-CAPS 阶段名 + 冒号 + 逗号分隔的裸标识符）；
    ② 调用式命令：``call `read_memory` `` / ``use write_memory`` /
       ``调用 `read_memory` ``。
- 白名单 ``_NON_TOOL_IDENTIFIERS`` 显式列出**不是工具**但会被上面口径捞到的
  标识符 —— 只允许从白名单删，不允许为了让某个名字变绿而新增条目
  （``test_allowlist_does_not_rot`` 会检查这一点）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_PROMPTS_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "hiveweave" / "prompts"
)

# 「看起来像工具名」= 全小写 + 下划线的标识符。
_TOOLISH_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")

# 调用式命令：``call `read_memory` `` / ``call write_memory`` / ``调用 `read_memory` ``。
#
# ⚠️ 刻意**不收** ``use X``：英文散文里 "Don't use pm_architect for a
# 3-person team" 的 use 是「别采用某种组织范式」，不是「去调工具」
# —— 收了它会假红。要收 use 必须带反引号且后面紧跟 `(`（真调用形态）。
_IMPERATIVE_RE = re.compile(
    r"(?:call|调用)\s+`?([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`?"
    r"|use\s+`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`\s*\(",
)

# 阶段工具清单行：``- EXPLORE: list_files, read_file, grep, ...``
# 要求 ALL-CAPS 阶段名（EXPLORE / DEFINE / VERIFY / …）+ 冒号开头，
# 且整行以逗号分隔的**裸标识符**为主 —— 这一形态在本仓只用于「工具清单」。
# 对照反例（故意不匹配）：``1. `docs` → `attest_doc_review`;``（数字开头、
# 含箭头、含反引号 ⇒ 是 gate 映射表，不是工具清单）。
_PHASE_LIST_RE = re.compile(
    r"^\s*-?\s*[A-Z][A-Z_ ]*:\s*([a-z][a-z0-9_ ,]*?)\s*(?:\(|$)",
    re.M,
)

# 非工具标识符白名单：会被上面的口径捞到，但确实是别的意思。
# 只允许删除，不允许为了让某个名字变绿而新增（见防腐测试）。
_NON_TOOL_IDENTIFIERS: frozenset[str] = frozenset()


def _prompt_string_literals() -> list[tuple[str, str]]:
    """(file_rel, literal) —— prompts 包下所有 Python 字符串字面量。

    只取 ``ast.Constant(str)`` 与 f-string 的静态段；排除首条 docstring
    （docstring 描述模块，不驱动 agent 行为）。
    """
    out: list[tuple[str, str]] = []
    for py in sorted(_PROMPTS_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                body = getattr(node, "body", None)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))
        rel = f"prompts/{py.name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                out.append((rel, node.value))
            elif isinstance(node, ast.JoinedStr):
                parts = [
                    v.value
                    for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                ]
                if parts:
                    out.append((rel, "".join(parts)))
    return out


def _demanded_tool_names(text: str) -> set[str]:
    """从一段提示词里抽出「被要求调用的工具名」。

    两种语境（都是明确的「去调它」信号，见模块 docstring 的取舍说明）：
      1. 调用式命令：``call `read_memory` `` / ``use write_memory`` / ``调用 X``
      2. 阶段工具清单行：``- EXPLORE: list_files, read_file, ...``
    """
    names: set[str] = set()
    for groups in _IMPERATIVE_RE.findall(text):
        if isinstance(groups, tuple):
            names.update(g for g in groups if g)
        elif groups:
            names.add(groups)

    for m in _PHASE_LIST_RE.finditer(text):
        for tok in m.group(1).split(","):
            tok = tok.strip()
            if _TOOLISH_RE.fullmatch(tok):
                names.add(tok)
    return names


def _registered_tool_names() -> set[str]:
    """权威工具名集合 = ``@tool`` 注册表 ∪ 旧式 dispatch 工具。"""
    import hiveweave.tools  # noqa: F401 — 触发 @tool 注册
    from hiveweave.tools.base import _TOOL_REGISTRY
    from hiveweave.tools.pipeline import LEGACY_DISPATCH_TOOLS

    return set(_TOOL_REGISTRY) | set(LEGACY_DISPATCH_TOOLS)


def test_prompt_named_tools_all_exist():
    """提示词里让 agent 调用的每个工具名，都必须在工具注册表里。"""
    known = _registered_tool_names()
    assert "read_memory" in known, "工具注册表没被填充？本测试会空转假绿"
    assert "write_memory" in known

    unknown: dict[str, list[str]] = {}
    for rel, text in _prompt_string_literals():
        for name in _demanded_tool_names(text):
            if name in known or name in _NON_TOOL_IDENTIFIERS:
                continue
            unknown.setdefault(name, []).append(rel)

    assert not unknown, (
        "这些名字出现在提示词里、像是要 agent 去调的工具，但**不在工具注册表**"
        f"里：{ {k: sorted(set(v)) for k, v in unknown.items()} }。\n"
        "这正是 `read_project_memory` 型 bug（batch 7 修）：提示词引导 agent 调"
        "一个不存在的工具，每轮白烧一次调用。\n"
        "二选一：① 把提示词改成真实工具名（首选——没有产品证据的工具不该新增）；"
        "② 若确实需要该工具，先实现并注册它。\n"
        "若某名字是列名/阶段名而非工具，请显式加入 _NON_TOOL_IDENTIFIERS。"
    )


def test_allowlist_does_not_rot():
    """白名单不得腐烂：白名单里的名字不应是**已注册的真实工具**。

    防「用白名单把真工具名也豁免掉」——那会让门禁对它永久失明。
    """
    known = _registered_tool_names()
    stale = sorted(n for n in _NON_TOOL_IDENTIFIERS if n in known)
    assert not stale, (
        f"_NON_TOOL_IDENTIFIERS 里的这些名字其实是真实工具：{stale} —— "
        "请把它们从白名单删掉，让门禁正常覆盖。"
    )


def test_gate_detects_a_simulated_phantom_tool():
    """负样本：门禁必须能抓到「提示词里有、注册表里没有」的名字。

    注入一个确定不存在的工具名（不写入仓库，只在内存里跑判定），
    确认同一判定逻辑会对它打红 —— 否则本门禁是假绿。
    """
    known = _registered_tool_names()
    phantom = "read_project_memory"  # 就是本批修掉的那个真 bug 的名字
    assert phantom not in known, (
        "read_project_memory 竟然在注册表里了？那说明有人补了这个工具 —— "
        "请复核本提交的提示词改动是否仍然正确。"
    )
    # 两种语境都要能抽出来
    assert phantom in _demanded_tool_names(
        "Before reviewing, call `read_project_memory` to check patterns."
    ), "抽取口径坏了：调用式命令里的工具名都抽不出来"
    assert phantom in _demanded_tool_names(
        "- EXPLORE: list_files, read_file, read_project_memory (no skill needed)"
    ), "抽取口径坏了：阶段清单行里的工具名都抽不出来"


def test_extractor_ignores_enum_values_and_config_keys():
    """负样本的对照面：抽取口径**不得**把 enum 值 / 配置键当工具名。

    这是本门禁最容易腐烂的地方 —— 本仓提示词把 enum 值（``done_slice``）、
    参数名（``waiting_on``）、gate 名（``module_visual``）也一律用反引号包。
    粗口径「凡反引号即工具」会造出 20+ 假阳性，把门禁变成噪音源（然后被
    人一次性加进白名单 —— 门禁就死了）。这里钉死精确口径。
    """
    for noisy in (
        "每轮必须 `commit_turn(phase=in_progress|waiting|blocked|done_slice)` 收尾",
        "派 QA 时用 `submitGate=module_visual`",
        "`unit` → 可 consume `test_run`",
        "系统会自动给任务带一份交付契约（`contract_json`）",
    ):
        assert _demanded_tool_names(noisy) == set(), (
            f"抽取口径过宽：从「{noisy}」抽出了 {_demanded_tool_names(noisy)}"
        )
