"""提示词 ↔ 代码 同步绊线（2026-09-11，TEST_DSH_50/51 审计轮）。

背景：本轮的 L13/L14/L15 都是「提示词或注释里写了一件代码里不成立的事」：

- **L13**：`prompts/coordinator.py` 写着「平台不对整轮写码设墙钟」，而代码**有**整轮兜底墙钟
  （`llm/streamer/core.py:164` 的 `asyncio.wait_for(HARD_TOTAL_TIMEOUT_S + 30.0)`；
  源码 1710+30、打包 EXE 570+30，同日实测 4 个 run 精确死在 600.0 s）。
  更麻烦的是它**诱导 agent 往错方向排查**（去猜「模型为什么不吐字」，而真正卡住的是它自己的命令）。
- **L14**：`prompts/qa_lead.py` 要求 QA「判根因归属」，却没告诉它平台已经会发
  `runner_failed` / `command_failed` 归因字段 —— 已有的事实位，agent 不知道它存在。
- **L15**：`db/schema.py` 声明 `timeout_kind` 取值域含 `runner`，全仓**无写入点**。

DSH 的做法（`packages/core/system-prompt/src/index.ts:121 SECTION_ORDERS`）印证了方向：
它的提示词段表里**没有「运行时事实」段**，对 `timeout|minutes|seconds|deadline` 零命中 ——
**会被配置/打包形态改变的量一律不进提示词**；提示词只写不随部署变化的契约。

本文件把这三条变成可机检的绊线，防止下一个人重新踩进去。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "hiveweave"
_PROMPTS = _SRC / "prompts"

# 会被「配置 / 打包形态 / 环境变量」改变的量，不该以**绝对化陈述**出现在提示词里。
# 注意：不禁止「数字+时间」本身（提示词里有大量合法的举例，如「等 5 分钟比 merge 冲突 2 小时便宜」），
# 只禁止**对平台自身属性的断言**——上一版那句「平台不对整轮写码设墙钟」正是这一类。
_FORBIDDEN_CLAIM_PATTERNS = [
    r"墙钟",              # 已清零；再出现即需人工复核（当前唯一一处已改为行为契约）
    r"不设.{0,6}超时",
    r"没有.{0,6}超时",
    r"无.{0,4}超时",
]

# 提示词里以反引号点名的 snake_case 标识符（字段 / 参数 / 工具名），
# 必须真实存在于源码里 —— 否则 agent 会去读一个不存在的字段或调一个不存在的工具。
#
# 第一版把要检查的名字**写死**成 ("runner_failed", "command_failed", "timeout_kind")，
# 结果「往提示词里加一个源码中不存在的事实位 `verdict_unknown`」这条回归**测不出来**
# （见 prove_prompt_guard.py 的第二轮：仍通过）。改成**从提示词里发现**名字。
_IDENT_IN_BACKTICKS = re.compile(r"`([a-z][a-z0-9_]{2,})`")

# 允许出现在提示词里、但按设计不在本仓源码中的名字（外部协议 / 上游约定）。
# 加条目必须带一行理由 —— 这个清单本身就是「提示词与代码的边界」的书面记录。
_EXTERNAL_IDENTIFIERS: dict[str, str] = {}


def _prompt_files() -> list[Path]:
    return sorted(p for p in _PROMPTS.glob("*.py") if p.name != "__init__.py")


def _source_files(*, exclude_prompts: bool = False) -> list[Path]:
    """源码文件。``exclude_prompts=True`` 时排除 prompts/ 本身 —— 见下面对该参数的说明。"""
    files = [p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts]
    if exclude_prompts:
        files = [p for p in files if "prompts" not in p.parts]
    return files


def test_prompts_do_not_assert_platform_runtime_attributes():
    """提示词不得对「平台的运行时属性」作绝对化陈述（L13 的绊线）。

    SCHEMA 依据：DSH `SECTION_ORDERS` 里没有「运行时事实」段；时限由运行时负责
    （`timeout-policy`），提示词只写「怎么做 / 遇到 X 算谁的 / 什么算 done」。
    """
    offenders: list[str] = []
    for f in _prompt_files():
        text = f.read_text(encoding="utf-8")
        for pat in _FORBIDDEN_CLAIM_PATTERNS:
            for m in re.finditer(pat, text):
                line_no = text[: m.start()].count("\n") + 1
                offenders.append(f"{f.name}:{line_no} 命中 /{pat}/")
    assert not offenders, (
        "提示词里出现了对平台运行时属性的绝对化陈述：\n  "
        + "\n  ".join(offenders)
        + "\n→ 会被配置/打包形态改变的量（超时、并发、预算、路径）一律不进提示词；"
        "改成行为契约（怎么做 / 遇到 X 算谁的），或只说明「通知里会带哪个字段」。"
    )


def test_identifiers_named_in_prompts_exist_in_source():
    """提示词里反引号点名的标识符必须真实存在于源码（L14 的绊线）。

    「提示词叫 agent 去看某个字段 / 调某个工具，但代码里没有这个东西」比不说更糟 ——
    agent 会照着去读一个不存在或恒空的字段，然后把「读不到」误判成「没问题」。

    发现方式：从提示词文本里**抽出**所有反引号 snake_case 标识符，逐个回源码核对。
    （不写死名单 —— 写死名单会让「新加一个假字段名」这条回归测不出来。）
    """
    prompt_text = "\n".join(f.read_text(encoding="utf-8") for f in _prompt_files())
    mentioned = sorted(set(_IDENT_IN_BACKTICKS.findall(prompt_text)))
    assert len(mentioned) > 20, (
        f"只从提示词里抽出 {len(mentioned)} 个标识符，明显偏少 —— 提示词格式可能变了，"
        "请检查 `_IDENT_IN_BACKTICKS` 是否仍然适用。"
    )

    # **必须排除 prompts/ 自己**：否则「提示词里写的名字」必然在 src_text 里出现（就是它自己），
    # 断言恒真 —— 这是本守卫第一版的实际 bug（prove_prompt_guard.py 第二轮把它照出来了：
    # 注入 `` `verdict_unknown` `` 后仍通过）。**检查语料与受检对象重叠 = 自我满足的断言。**
    src_text = "\n".join(
        f.read_text(encoding="utf-8") for f in _source_files(exclude_prompts=True)
    )
    missing = [
        name for name in mentioned
        if name not in _EXTERNAL_IDENTIFIERS
        and not re.search(rf"\b{re.escape(name)}\b", src_text)
    ]
    assert not missing, (
        f"提示词里点名了源码中不存在的标识符：{missing}\n"
        "→ 要么是拼错/编造的字段名（agent 会照读、照失败），要么是外部协议名 ——"
        "后者请加进 `_EXTERNAL_IDENTIFIERS` 并写明理由。"
    )


_TESTS_DIR = Path(__file__).resolve().parent


def test_verbatim_asserted_prompt_blocks_survive_punctuation_normalization():
    """被测试**逐字断言**的角色块，必须在 `_normalize_cjk_punct` 下不变。

    背景（2026-09-11 真实回归）：往 `prompts/qa_lead.py` 加了一句话，里面用了 `——`（U+2014 ×2）。
    而 `identity.py::_normalize_cjk_punct` 会把 `——` 换成 `--`（`_CJK_PUNCT_FIX`），
    于是 `tests/test_qa_lead_seed.py:77` 的 `assert QA_LEAD_BLOCK in qa_prompt` **逐字比对失败** ——
    全量跑出 `1 failed, 3635 passed`。**块里写的字符，会被注入时的归一化改写。**

    注意：本仓大量块（`identity.py` 的 `_REALITY_BLOCK` 等）**有意**使用 `「」` / `——` 并依赖归一化，
    所以**不能**对所有块要求幂等。这里只覆盖「测试会逐字比对」的那些块 ——
    名单从 `tests/` 里**发现**（扫 `<NAME> in …` 形式），新增逐字断言会自动纳入。
    """
    from hiveweave.prompts.identity import _normalize_cjk_punct

    tests_text = "\n".join(p.read_text(encoding="utf-8") for p in _TESTS_DIR.glob("*.py"))
    names = sorted(set(re.findall(r"\b([A-Z][A-Z0-9_]*BLOCK)\b\s+in\b", tests_text)))
    assert names, (
        "没在 tests/ 里发现任何「<BLOCK> in <prompt>」形式的逐字断言 —— "
        "若确实删光了这类断言，请一并删除本测试；否则检查正则。"
    )

    # 从 prompts/ 模块里取这些常量的实际值
    import importlib

    values: dict[str, str] = {}
    for f in _PROMPTS.glob("*.py"):
        if f.name == "__init__.py":
            continue
        mod = importlib.import_module(f"hiveweave.prompts.{f.stem}")
        for n in names:
            v = getattr(mod, n, None)
            if isinstance(v, str):
                values[n] = v

    missing = [n for n in names if n not in values]
    assert not missing, f"测试里断言了这些块，但 prompts/ 里找不到同名常量：{missing}"

    bad = [n for n, v in values.items() if _normalize_cjk_punct(v) != v]
    assert not bad, (
        f"这些块被测试逐字断言，但含有会被 `_normalize_cjk_punct` 改写的字符：{bad}\n"
        "→ 注入后提示词里的字符会被替换（如 `——`→`--`、`「」`→`\"`），逐字断言必然失败。\n"
        "→ 在这类块里请直接写归一化后的形态（用 `--` 而不是 `——`，用 ASCII 引号）。"
    )


def test_declared_timeout_kind_values_have_writers_or_are_marked_reserved():
    """`timeout_kind` 的取值域声明必须与代码对齐（L15 的绊线）。

    规则：`db/schema.py` 注释里声明的每个取值，要么在源码里有写入点，
    要么在同一注释里被显式标注「预留」——不允许「声明了却既没接线也没标注」。
    """
    schema = (_SRC / "db" / "schema.py").read_text(encoding="utf-8")
    block = re.search(
        r"# 取值域（[^）]*）[：:]\n(?P<body>(?:    #.*\n)+?)\s*\"\"\"ALTER TABLE run_steps ADD COLUMN timeout_kind",
        schema,
    )
    assert block, "未在 db/schema.py 找到 timeout_kind 的取值域注释块（若已重写，请同步本测试）"
    body = block.group("body")

    declared: list[tuple[str, str]] = []
    for line in body.splitlines():
        m = re.match(r"\s*#\s+([a-z_]+)\s+—\s+(.*)$", line)
        if m:
            declared.append((m.group(1), line))
    assert declared, "取值域注释块里没解析出任何取值（注释格式可能变了）"

    src_text = "\n".join(f.read_text(encoding="utf-8") for f in _source_files())
    problems = []
    for value, raw_line in declared:
        has_writer = re.search(rf'timeout_kind\s*[:=]\s*"{value}"', src_text) or re.search(
            rf"timeout_kind\s*=\s*'{value}'", src_text
        )
        marked_reserved = "预留" in raw_line
        if not has_writer and not marked_reserved:
            problems.append(value)
    assert not problems, (
        f"timeout_kind 声明了 {problems} 但既无写入点、也未标「预留」"
        "→ 要么接线，要么删掉该取值。"
    )
