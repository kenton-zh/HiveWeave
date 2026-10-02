"""批 5 防复发绊线 —— prompts/*.py 的四类漂移绊线（数值/能力/契约/通道）。

背景：`skills/prompt-authoring/SKILL.md` §0 一句话 —— 提示词里每个事实有且仅有
一个所有者；owner 是代码/回执/schema 的，prose 里就该消失。本仓 4 天 42 提交的
9 处缺口全部是「平台已知的事实被手抄进散文，然后漂移」（90s vs 540s、`:483`、
「报错里的 [shared fix]」等）。`test_prompt_acl_wording_sync.py` 把 **ACL 措辞**
一类钉成了绊线；本文件把同一范式扩到**四类**，每类给**机检判据**：

  1. **数值类** —— 扫渲染输出的时长/上限 token，未知数值直接红（逼作者登记
     owner 常量路径）；已知数值与其 owner 常量**现场对账**（常量改了提示词没跟
     ⇒ 红，反之亦然）；批 2 已修的「硬顶 90s」**无条件钉死**（连登记都不许）。
  2. **能力类** —— 渲染文本里反引号包的 snake_case 标识符必须 ∈ 已注册工具
     ∪ 非工具白名单（抓 `read_project_memory` 型幻影工具的**变体**：调用式
     命令之外的裸点名）；各 family（CEO/HR/coordinator/executor）渲染文本里
     **点名**的已注册工具若不在该 family 的 permission allowlist，其差集对
     基线**只许缩不许涨**（抓「:483 型」矛盾重演：allowlist 移除了提示词还在
     点名的工具 ⇒ 红）。
  3. **契约类** —— prompts 中出现的 `FactKind`/`timeout_kind`/`WaitingKind`/
     submitGate/凭证 kind/verdict/evidence_kind/审计软失败 reason 等枚举值
     必须 ⊆ 后端枚举（枚举的 owner 是后端，prompts 只是消费方）；identity 的
     waiting_on kind 枚举句与 `turn_result.WaitingKind` **逐值一致**；
     `DENIED_BY_SIGNATURES` 的 kind 若被提示词点名，必须 ∈ `DENIED_BY_KINDS`。
  4. **通道类** —— prompts 里出现的 `[XXX]` 平台标记必须能在后端找到**真实
     构造点**（canonical：`services/inbox.py::ACK_SPARE_PREFIXES`；其余逐个
     钉到构造文件并现场验证字面量仍在）；`[shared fix]` 不得再说成「在报错
     里/回执 error 字段」（批 2 钉死形态：独立收件箱提示）；`[TURN EXIT
     BLOCKED]` 字样须与 `services/turn_exit.py` / `agents/completion.py` 的
     常量形态一致。

**基线维护**：`tests/_prompt_sync_tripwires_b5.json`。

    # 清债（family 差集缩小，没有新增）——正常路径
    cd apps/hiveweave-py && uv run python -m tests.test_prompt_sync_tripwires_b5 --write
    # 有意登记「新增」（family 差集变大 / 新增无构造点标记）——必须显式说明
    cd apps/hiveweave-py && uv run python -m tests.test_prompt_sync_tripwires_b5 --write --allow-grow

基线只管两类**会增长的集合**：`family_tool_gap`（机器扫描，只许缩）与
`channel_unconstructed_markers`（手写登记，现存违规注记 owner/待办，修源后删除）。
数值 allowlist / 非工具白名单在**本文件内**（带 owner 注记的代码，review 可见），
新增条目 = 改代码 = 自然被 review 看到，不入 JSON。

**本文件不守什么（必须说清）**：
  - 数值网只扫**时长与行数上限** token（正则见 `_DUR_RE`/`_LINE_RE`）；
    人数（≤7 / 30 人）、字数（2-4 句）、张数/MB（message_user 图片）等**非时间
    非行数**上限不在网内 —— 它们各有 owner（permission/policy/schema），且
    prompts 已自带「平台不拦」声明，罩进来只会造噪音。
  - 能力网只认**反引号内**的 snake_case 标识符；裸英文词（browse/read）与
    「否定语境」的语义判别不做（`CEO 仍无 run_tests` 这类合法否定靠 family
    差集**基线**吸收，而不是靠解析否定词 —— 否定词解析是文本判据，会漂）。
  - 契约网只查 prompts **消费**的枚举值；后端枚举自身的闭合性由各 owner
    模块的 import 期断言与各自测试守（本文件只做 **⊆/== 方向**的同步检查）。
  - 通道网的真值表 = `ACK_SPARE_PREFIXES` ∪ 本文件 `_CHANNEL_MARKER_SOURCES`
    （构造点锚定）。后端**新增**投递标记而 prompts 不提，不归本文件管（那是
    提示词同步问题，由 review 抓）。

**纪律**：与全仓一致 —— 判据尽量落在**导入的后端常量/枚举**上（状态判据），
渲染文本里的正则只负责「抽取候选」，候选的**裁决**一律对后端真值做。
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path
from typing import get_args

# ── 后端真值源（状态判据；全部现场导入，常量漂移即红）─────────────────
import hiveweave.tools  # noqa: F401 —— 触发 @tool 注册
from hiveweave.services.attestation import POLICY_REQUIRED_KINDS
from hiveweave.services.charter import ORG_SHAPE_MARKER_PREFIX
from hiveweave.services.code_audit import _SOFT_FAIL_ATTEMPT_REASONS
from hiveweave.services.delivery_contract import EVIDENCE_KIND_NOT_APPLICABLE
from hiveweave.services.inbox import ACK_SPARE_PREFIXES
from hiveweave.services.permission import (
    CEO_TOOLS,
    COORDINATOR_BUILDER_TOOLS,
    EXECUTOR_BASE_TOOLS,
    HR_TOOLS,
)
from hiveweave.services.tasks.policy import _KIND_SOURCE_TOOL
from hiveweave.services.tasks.verify import normalize_verdict
from hiveweave.services.turn_result import WaitingKind
from hiveweave.tools.base import _TOOL_REGISTRY
from hiveweave.tools.bash import TOOL_DEFAULT_TIMEOUT_MS
from hiveweave.tools.fact_positions import DENIED_BY_SIGNATURES
from hiveweave.tools.pipeline import LEGACY_DISPATCH_TOOLS
from hiveweave.tools.question import QUESTION_TIMEOUT_S
from hiveweave.tools.result import (
    _BLOCKED_FACT_KINDS,
    DENIED_BY_KINDS,
    FACT_KINDS,
)

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "hiveweave"
_PROMPTS_ROOT = _SRC_ROOT / "prompts"
_BASELINE_PATH = Path(__file__).with_name("_prompt_sync_tripwires_b5.json")

WAITING_KINDS: frozenset[str] = frozenset(get_args(WaitingKind))


# ── 渲染矩阵 ─────────────────────────────────────────────────────────
# identity 渲染 = 共享段 + 角色剧本，是 agent 实际看到的第一条 system 消息。
# 本仓 prompts 的消费形态就是它，所以四类网全部跑在**渲染输出**上（源码级
# 正则只给 [shared fix] 通道判别用 —— 它要按行看否定语境）。

_ROLE_MATRIX: tuple[tuple[str, str], ...] = (
    ("CEO", "coordinator"),
    ("HR", "coordinator"),
    ("qa_lead", "coordinator"),
    ("developer", "coordinator"),
    ("developer", "executor"),
    ("test_engineer", "executor"),
    ("code_reviewer", "executor"),
    ("security_auditor", "executor"),
    ("web_perf_auditor", "executor"),
    ("审查员", "executor"),
)

_FAMILY_ROLES: dict[str, tuple[str, ...]] = {
    # family 名与 services/permission.py 的集合名对齐（小写化）
    "ceo": ("CEO",),
    "hr": ("HR",),
    "coordinator_builder": ("qa_lead", "developer"),  # coordinator 权限族
    "executor_base": (
        "developer",
        "test_engineer",
        "code_reviewer",
        "security_auditor",
        "web_perf_auditor",
        "审查员",
    ),
}

_FAMILY_SETS: dict[str, frozenset[str]] = {
    "ceo": frozenset(CEO_TOOLS),
    "hr": frozenset(HR_TOOLS),
    "coordinator_builder": frozenset(COORDINATOR_BUILDER_TOOLS),
    "executor_base": frozenset(EXECUTOR_BASE_TOOLS),
}

_RENDER_CACHE: dict[str, str] = {}


def _render(role: str, role_type: str) -> str:
    key = f"{role}::{role_type}"
    if key not in _RENDER_CACHE:
        from hiveweave.prompts import build_identity_prompt

        _RENDER_CACHE[key] = build_identity_prompt(role, role_type, "", name="测试")
    return _RENDER_CACHE[key]


def _rendered_all() -> str:
    return "\n\n====\n\n".join(_render(r, rt) for r, rt in _ROLE_MATRIX)


def _prompt_source_files() -> list[Path]:
    return sorted(p for p in _PROMPTS_ROOT.glob("*.py") if p.name != "__init__.py")


def _prompts_source_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _prompt_source_files())


def _src_text(rel: str) -> str:
    return (_SRC_ROOT / rel).read_text(encoding="utf-8")


def _registered_tool_names() -> set[str]:
    """工具名唯一权威源 = @tool 注册表 ∪ legacy dispatch 工具。"""
    return set(_TOOL_REGISTRY) | set(LEGACY_DISPATCH_TOOLS)


# ── 抽取器 ───────────────────────────────────────────────────────────

# 反引号内的 snake_case 标识符（含 `tool(param=...)` 调用形态的前缀）。
_BACKTICK_SNAKE_RE = re.compile(r"`([a-zA-Z][a-zA-Z0-9]*(?:_[a-zA-Z0-9]+)+)")

# 时长 token：数字 + 时间单位。先抓区间（30–90s 的两端都算数），再抓单值。
# 拉丁单位必须带词尾边界（否则「2-3 sentences」的 s 会被当成秒 —— 实测翻车）；
# CJK 单位不加（钟/秒 后面紧跟汉字时无 \b）。
_UNITS_LATIN = r"(?:ms|secs?|seconds?|mins?|minutes?|hours?|s)"
_UNITS_CJK = r"(?:小时|分钟|秒)"
_DUR_RANGE_RE = re.compile(
    rf"(\d+(?:\.\d+)?)\s*[–—-]\s*(\d+(?:\.\d+)?)\s*({_UNITS_LATIN}\b|{_UNITS_CJK})"
)
_DUR_RE = re.compile(rf"(\d+(?:\.\d+)?)\s*({_UNITS_LATIN}\b|{_UNITS_CJK})")

# 行数上限 token。
_LINE_RE = re.compile(r"(\d+)\s*(?:lines?|行)")

_UNIT_NORMALIZE: dict[str, str] = {
    "ms": "ms",
    "s": "s",
    "sec": "s",
    "secs": "s",
    "second": "s",
    "seconds": "s",
    "min": "min",
    "mins": "min",
    "minute": "min",
    "minutes": "min",
    "小时": "小时",
    "hour": "小时",
    "hours": "小时",
    "分钟": "分钟",
    "秒": "秒",
}


def _dur_key(value: str, unit: str) -> str:
    norm = _UNIT_NORMALIZE.get(unit)
    if norm is None:
        raise KeyError(f"未知时间单位 {unit!r} —— 请同步 _UNIT_NORMALIZE")
    return f"{value}{norm}"


def _extract_duration_keys(text: str) -> dict[str, int]:
    """渲染文本里的时长 token → 出现次数（区间两端各自计一次）。"""
    keys: dict[str, int] = {}
    consumed: list[tuple[int, int]] = []
    for m in _DUR_RANGE_RE.finditer(text):
        unit = _UNIT_NORMALIZE[m.group(3)]
        for v in (m.group(1), m.group(2)):
            keys[f"{v}{unit}"] = keys.get(f"{v}{unit}", 0) + 1
        consumed.append(m.span())
    for m in _DUR_RE.finditer(text):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        key = _dur_key(m.group(1), m.group(2))
        keys[key] = keys.get(key, 0) + 1
    return keys


# ══ 1. 数值类 ═══════════════════════════════════════════════════════


class _NumEntry(types.SimpleNamespace):
    """owner 注记 + 可选机检（value: float -> bool）。check=None = 纯修辞，无机制 owner。"""


# 时长 allowlist：**新数值先在这里登记（带 owner 常量路径）再放行**。
# check 用 lambda 现场读后端常量 —— 常量改了提示词没跟（或反向）当场红。
# （2026-09-29 逐提交审计门拆除：code audit 时耗修辞 600s/90s/30s/540s 已随
# 审计节一并退出 prompts，条目删除；审计时延的 owner 回归工具回执。）
_DURATION_ALLOWLIST: dict[str, _NumEntry] = {
    "180s": _NumEntry(
        owner="tools/question.py QUESTION_TIMEOUT_S（question 轮内钟）",
        check=lambda v: v == QUESTION_TIMEOUT_S,
    ),
    "10min": _NumEntry(
        owner="tools/bash.py TOOL_DEFAULT_TIMEOUT_MS['bash'] == 600_000",
        check=lambda v: v * 60 == TOOL_DEFAULT_TIMEOUT_MS["bash"] // 1000,
    ),
    "2.8s": _NumEntry(
        owner="identity 通信效率段的 LCP **示例**（教格式，非机制值）",
        check=None,
    ),
    "2.5s": _NumEntry(
        owner="同上（LCP target 示例值）",
        check=None,
    ),
    "5分钟": _NumEntry(
        owner="修辞：executor 模块边界段「等 5 分钟比 merge 冲突 2 小时便宜」",
        check=None,
    ),
    "2小时": _NumEntry(
        owner="修辞：同句的冲突代价对比",
        check=None,
    ),
}

# 行数 allowlist（同机制）。
# （2026-09-29 逐提交审计门拆除：20 行审计阈值不再进 prompts，"20" 条目删除；
# 行数阈值的 owner 回归 services/code_audit.py，由该模块自己的测试守。）
_LINE_ALLOWLIST: dict[str, _NumEntry] = {
    "100": _NumEntry(
        owner="修辞：code_reviewer 剧本「超 ~100 行建议拆分」（平台不拦）",
        check=None,
    ),
    "1": _NumEntry(
        owner="修辞：通信效率段「1-2 lines」的建议篇幅",
        check=None,
    ),
    "2": _NumEntry(
        owner="修辞：同上",
        check=None,
    ),
}


def test_numeric_hard_cap_90s_is_dead():
    """批 2 钉死项：`硬顶 90s` / `hard cap 90s` 无条件禁绝 —— 登记基线也不许。

    缺口 2 的病灶形态 = 「典型值 90s 被写成硬顶」（实际 540s→600s）。典型值与
    硬顶是两个语义位，把典型值写进 cap 位置 = 提示词教 agent 等一个平台根本
    不存在的上限。所以这条**不设基线出口**：源码与渲染输出双扫，命中即红。
    """
    banned = re.compile(r"(硬顶|硬上限|hard\s*cap)\s*[:：]?\s*90(?!\d)", re.IGNORECASE)
    offenders: list[str] = []
    src = _prompts_source_text()
    for m in banned.finditer(src):
        offenders.append(f"source: …{src[max(0, m.start() - 30):m.end() + 10]!r}…")
    rendered = _rendered_all()
    for m in banned.finditer(rendered):
        offenders.append(f"rendered: …{rendered[max(0, m.start() - 30):m.end() + 10]!r}…")
    assert not offenders, (
        "prompts 里出现「硬顶 90s / hard cap 90s」形态（批 2 已修的缺口 2 复发）。"
        "典型耗时与工具级硬顶是两个语义位：硬顶数字必须模板注入/与 "
        "declared_timeout_s('request_code_audit') 同源，典型值不得写进 cap 位。\n  "
        + "\n  ".join(offenders)
    )


def test_numeric_duration_tokens_all_registered():
    """未知时长 token 直接红 —— 新数值必须先在 _DURATION_ALLOWLIST 登记 owner。"""
    keys = _extract_duration_keys(_rendered_all())
    unknown = {k: n for k, n in keys.items() if k not in _DURATION_ALLOWLIST}
    assert not unknown, (
        f"渲染提示词出现未登记的时长 token：{unknown}。\n"
        "数值的 owner 必须是代码常量（模板注入）或回执；若确属散文修辞"
        "（无常量可指），在 tests/test_prompt_sync_tripwires_b5.py 的 "
        "_DURATION_ALLOWLIST 登记条目并写明 owner=修辞（无常量）+ 为什么"
        "不会漂移。不许为变绿而 silent 加条目。"
    )


def test_numeric_duration_owners_match_constants():
    """已登记数值与其 owner 常量**现场对账**（check=None 的修辞条目跳过）。"""
    keys = _extract_duration_keys(_rendered_all())
    failures: list[str] = []
    for key, n in sorted(keys.items()):
        entry = _DURATION_ALLOWLIST.get(key)
        if entry is None or entry.check is None:
            continue
        value = float(re.match(r"([\d.]+)", key).group(1))
        try:
            ok = bool(entry.check(value))
        except Exception as exc:  # noqa: BLE001 —— owner 常量读不到也要报成对账失败
            ok = False
            failures.append(f"{key}（×{n}）owner 常量读取失败：{exc!r}")
            continue
        if not ok:
            failures.append(
                f"{key}（×{n}）与 owner 不一致：{entry.owner}"
            )
    assert not failures, (
        "提示词里的时长数值与 owner 常量漂移（两边只该有一个 owner：改了常量"
        "就同步提示词，或改提示词数字前先改常量）：\n  " + "\n  ".join(failures)
    )


def test_numeric_line_thresholds_all_registered_and_synced():
    """行数上限 token：登记 + 与 owner 常量对账（check=None 的修辞条目跳过）。"""
    rendered = _rendered_all()
    found = {m.group(1) for m in _LINE_RE.finditer(rendered)}
    unknown = sorted(found - set(_LINE_ALLOWLIST))
    assert not unknown, (
        f"渲染提示词出现未登记的行数上限 token：{unknown} —— "
        "在 _LINE_ALLOWLIST 登记 owner 常量路径（无常量可指的修辞也要注记）。"
    )
    drifted = []
    for key, entry in _LINE_ALLOWLIST.items():
        if entry.check is None or key not in found:
            continue
        if not entry.check(float(key)):
            drifted.append(f"{key} 行 ≠ {entry.owner}")
    assert not drifted, "行数上限与 owner 常量漂移：\n  " + "\n  ".join(drifted)


def test_numeric_extractor_is_not_vacuous():
    """尺子自检：数值网今天必须能看到这些已知 token（否则网已失效）。

    2026-09-29 逐提交审计门拆除后，code audit 时耗修辞（600s/90s/30s/540s）
    与 20 行审计阈值已退出 prompts —— 锚点换成仍存活的 token；若这里再红，
    说明锚点文案又被重写，先修尺子再谈绊线。
    """
    keys = _extract_duration_keys(_rendered_all())
    for must in ("2.8s", "2.5s", "5分钟"):
        assert must in keys, (
            f"数值抽取器看不到已知的 {must} —— 提取正则坏了，或 prompts 被"
            "整体重写。先修尺子再谈绊线（否则本文件其余数值网全是假绿）。"
        )
    lines = {m.group(1) for m in _LINE_RE.finditer(_rendered_all())}
    assert "100" in lines, "行数网看不到 code_reviewer 的 ~100 行 —— 同上，先修尺子。"


# ══ 2. 能力类 ═══════════════════════════════════════════════════════

# 非工具白名单：渲染文本反引号里的 snake_case 词，但**不是工具**（enum 值 /
# 参数名 / 字段名 / 出口门名 / 系统目录名）。只允许删除，不许为变绿乱加
# （rot-guard 见 test_capability_whitelist_does_not_rot）。
_NON_TOOL_WHITELIST: dict[str, str] = {
    # —— TurnResult / 回合出口枚举 ——
    "done_slice": "TurnPhase 值（services/turn_result.py）",
    "in_progress": "TurnPhase 值",
    "waiting_on": "TurnResult 字段名",
    "timeout_kind": "run_steps 字段名",
    # —— 状态机 / 出口门名 ——
    "ASSIGNEE_MUST_SUBMIT": "出口门违规码",
    "WAIT_WITHOUT_ASK": "出口门违规码",
    "HIRE_UNREPORTED": "出口门违规码",
    "DONE_TRUNCATED": "[SUBAGENT DONE_TRUNCATED] 标记的组成部分",
    # —— 任务/凭证域 enum 与字段 ——
    "code_audit": "submitGate/policy id 值",
    "code_audit_unit": "policy id 值",
    "docs_only": "policy id 值",
    "module_visual": "submitGate 值",
    "test_run": "attestation kind 值",
    "acceptance_coverage": "evidence 字段名",
    "evidence_kind": "deliveryContract 字段名",
    "not_applicable_reason": "deliveryContract 字段名",
    "contract_json": "任务行字段名",
    "attestation_id": "回执字段名",
    "artifact_refs": "dispatch_task 参数名",
    "already_merged": "git_worktree_merge 幂等语义值",
    "root_cause": "lessons 扩展字段名",
    "assignee_execution": "get_platform_state 字段名",
    "tool_outputs": ".hiveweave 系统子目录名",
    # —— code audit 软失败 reason（services/code_audit.py）——
    "llm_failed": "audit 软失败 reason 值",
    "no_model": "audit 软失败 reason 值",
    "no_callback": "audit 软失败 reason 值",
    "no_worktree": "audit 软失败 reason 值",
    # —— FactKind 值（tools/result.py）——
    "runner_failed": "FactKind 值",
    "command_failed": "FactKind 值",
}


def _backtick_snake_tokens(text: str) -> set[str]:
    return set(_BACKTICK_SNAKE_RE.findall(text))


def test_capability_backtick_toolish_tokens_are_real_tools_or_whitelisted():
    """渲染文本里反引号包的 snake_case 词 ∈ 注册表 ∪ 非工具白名单。

    `test_prompts_only_name_real_tools` 抓的是「call X / - PHASE: a, b, c」
    两种**调用式**语境；本条补缺口：裸点名（叙述里顺嘴一提的工具名）也必须
    真实存在 —— 幻影名字换一种句式出现时同样每轮白烧一次调用。
    """
    known = _registered_tool_names()
    assert "read_memory" in known and "bash" in known, "工具注册表没被填充？"
    union = _backtick_snake_tokens(_rendered_all())
    unknown = sorted(
        t for t in union if t not in known and t not in _NON_TOOL_WHITELIST
    )
    assert not unknown, (
        f"渲染提示词出现「工具名形态」却既不是注册工具也不在白名单的名字：{unknown}。\n"
        "这正是 read_project_memory 型幻影工具的变体路径。二选一："
        "① 改成真实工具名；② 若确属 enum 值/参数名/字段名，在 "
        "_NON_TOOL_WHITELIST 登记并注记它是什么（不许 silent 加）。"
    )


def test_capability_whitelist_does_not_rot():
    """白名单防腐：条目不许变成真实工具（否则门禁对它永久失明）。"""
    known = _registered_tool_names()
    stale = sorted(t for t in _NON_TOOL_WHITELIST if t in known)
    assert not stale, (
        f"_NON_TOOL_WHITELIST 里这些名字已经是注册工具：{stale} —— "
        "请从白名单删掉，让能力网正常覆盖。"
    )


def test_capability_family_named_tool_gap_does_not_grow():
    """:483 型矛盾绊线：family 提示词点名、而 family allowlist 没有的工具差集只许缩。

    基线 = 当前差集（含「CEO 仍无 run_tests」这类**合法否定点名**——本测试
    刻意不做否定词解析，那是会漂的文本判据；差集基线吸收它们）。此后：
      · allowlist 移除一个仍被提示词点名的工具 ⇒ 差集变大 ⇒ 红（:483 重演）；
      · 提示词新点名一个 family 没有的工具 ⇒ 红（须改提示词或 --allow-grow 登记）；
      · 清掉点名或补授权 ⇒ 差集缩 ⇒ 允许（--write 即可）。
    """
    baseline = _baseline()
    known = _registered_tool_names()
    failures: list[str] = []
    for family in _FAMILY_ROLES:
        gap = _compute_family_gap(family, known)
        frozen = sorted(baseline.get("family_tool_gap", {}).get(family, []))
        grew = sorted(set(gap) - set(frozen))
        if grew:
            failures.append(
                f"{family}: 新增未授权点名 {grew}（基线 {len(frozen)} 条 → 现 {len(gap)} 条）"
            )
    assert not failures, (
        "family 提示词点名了该 family allowlist 里没有的工具（:483 型矛盾）：\n  "
        + "\n  ".join(failures)
        + "\n→ 二选一：① 改提示词（删点名或改成「你没有 X」的准确表述）；"
        "② 确属该 family 应有的能力 → 补 permission.py 的 family 集合（提示词"
        "与授权同 commit）。若是否定语境的新点名（「仍无 X」），用 --allow-grow "
        "登记并在提交信息里写明语境。"
    )


def test_capability_family_scanner_is_not_vacuous():
    """尺子自检：family 差集扫描今天必须能看到这些已知锚点。"""
    known = _registered_tool_names()
    ceo_text = _render("CEO", "coordinator")
    ceo_named = {t for t in _backtick_snake_tokens(ceo_text) if t in known}
    assert "run_tests" in (ceo_named - _FAMILY_SETS["ceo"]), (
        "CEO 渲染里看不到已知的「run_tests 无权」点名 —— family 扫描器坏了。"
    )
    assert {"hire_agent", "bash_main", "waive_attestation"} <= ceo_named, (
        "CEO 点名集合缺已知锚点 —— 扫描器坏了。"
    )
    hr_named = {t for t in _backtick_snake_tokens(_render("HR", "coordinator")) if t in known}
    assert "hire_agent" in hr_named, "HR 扫描器坏了（hire_agent 必然被点名）。"
    assert "bash_main" in ceo_named, (
        "CEO 扫描器坏了（行政边界段点名 bash_main/pwsh_main）—— 注意 CEO 剧本"
        "从不裸点 `bash`（只点 bash_main），锚要用 bash_main。"
    )


# ══ 3. 契约类 ═══════════════════════════════════════════════════════


def test_contract_fact_kind_mentions_match_backend():
    """prompts 点名的 FactKind 值 ⊆ tools/result.py FACT_KINDS，且平台/己方归因一致。

    qa_lead 剧本教 QA 用 `runner_failed`（平台侧，墙的问题）/`command_failed`
    （交付质量问题）判根因归属 —— 若平台把某值挪出 `_BLOCKED_FACT_KINDS`
    （= 不再是「平台护栏拒绝」），qa_lead 的归因教学就反了，当场红。
    """
    rendered = _backtick_snake_tokens(_rendered_all())
    mentioned = sorted(rendered & FACT_KINDS)
    assert mentioned, "渲染提示词看不到任何 FactKind 点名 —— 尺子失效。"
    # rendered 里形如 FactKind 的四个词如果被点名却不 ∈ FACT_KINDS（平台改了
    # 枚举名而提示词没跟），就会在这里红：
    factish = {"runner_failed", "command_failed", "bad_args", "outcome_unknown"}
    stale = sorted(t for t in factish if t in rendered and t not in FACT_KINDS)
    assert not stale, f"prompts 点名了不存在的 FactKind 值：{stale}"
    if "runner_failed" in rendered:
        assert "runner_failed" in _BLOCKED_FACT_KINDS, (
            "qa_lead 剧本把 runner_failed 教成「平台侧（墙的问题）」，但它已不在 "
            "_BLOCKED_FACT_KINDS（平台护栏拒绝格）—— 归因教学与 blocked 语义反向了。"
        )


def test_contract_timeout_kind_values_exist_in_backend():
    """prompts 里的 timeout_kind 取值 ⊆ 后端实际写过的值集合。

    后端没有 TIMEOUT_KINDS 常量（F7 的值散在构造点），所以真值集 = 扫 src
    全部 `timeout_kind ... = "X"` 赋值/传参字面量。prompts（coordinator 剧本
    「若你的回合被掐断」段）点名的 turn/command 必须落在其中；反之 prompts
    在 timeout_kind 句里编一个后端从没写过的值（如写错成 `time_out`）⇒ 红。
    候选 = timeout_kind 行上的反引号 snake token，扣除字段名本身 / 已登记的
    非工具词 / 注册工具名（防把同段其他叙事词误当 kind 值）。
    """
    universe: set[str] = set()
    scan_re = re.compile(r"""timeout_kind["']?\]?\s*={1,2}\s*["']([a-z_]+)["']""")
    for py in _SRC_ROOT.rglob("*.py"):
        if _PROMPTS_ROOT in py.parents:
            continue
        universe.update(scan_re.findall(py.read_text(encoding="utf-8")))
    assert {"turn", "command", "wait"} <= universe, (
        f"timeout_kind 真值扫描退化（{sorted(universe)}）—— 构造点正则或源码布局变了。"
    )
    known = _registered_tool_names()
    candidates: set[str] = set()
    # timeout_kind 的取值是**单词**（turn/command/wait），不是 snake_case ——
    # 这里用「整段反引号内容 = 纯小写标识符」的口径（`bash(cmd=…)` 这类调用
    # 形态内容含括号，天然不命中）。
    tok_re = re.compile(r"`([a-z_][a-z0-9_]*)`")
    for line in _rendered_all().splitlines():
        if "timeout_kind" not in line:
            continue
        candidates.update(tok_re.findall(line))
    candidates -= {"timeout_kind"}
    candidates -= set(_NON_TOOL_WHITELIST)
    candidates -= known
    stale = sorted(candidates - universe)
    assert candidates, (
        "timeout_kind 句里抽不到任何候选取值 —— 尺子失效。"
    )
    assert not stale, (
        f"prompts 点名的 timeout_kind 取值 {stale} 不在后端写过的值集合 "
        f"{sorted(universe)} —— 取值 owner 是后端，prompts 不得发明。"
    )


def test_contract_waiting_on_kinds_match_turn_result():
    """prompts 的等待 kind 面不得发明后端没有的取值（T16 二期改指针式）。

    旧版钉「identity 枚举句 == turn_result.WaitingKind 逐值一致」；T16 二期
    commit_turn 提示词教学退役后，合法 kind 的 owner 位移到 ``wait_on`` 工具
    schema（tools/wait_tools.py 的 WaitOnItem，窄集 task/agent/fact），identity
    共享段只留指针句、不再散文枚举。本绊线随之改钉两层：
      ① 指针句在场（kind 契约的散文 owner 必须唯一且指向 wait_on 工具说明）；
      ② 散落点名（kind:agent / kind=task / waitKind="external" ...）⊆
        WaitingKind —— prompts 只许消费后端枚举，不许发明。
    """
    rendered = _rendered_all()
    assert (
        "合法 kind 与条目形状以 `wait_on` 工具说明与校验回执为准" in rendered
    ), (
        "identity 的 wait_on kind 指针句消失 —— 合法等待 kind 的散文 owner "
        "必须唯一指向 wait_on 工具说明（T16 二期），不得回退成散文枚举。"
    )
    # 散落点名（kind:agent / kind=task / waitKind="external" ...）⊆ WaitingKind
    mentioned: set[str] = set()
    mentioned.update(re.findall(r"\bkind\s*[:=]\s*[\"']?([a-z]+)", rendered))
    mentioned.update(re.findall(r"waitKind\s*=\s*[\"']([a-z]+)", rendered))
    stale = sorted(mentioned - WAITING_KINDS)
    assert not stale, (
        f"prompts 点名的 waiting_on/waitKind 取值 {stale} 不在 WaitingKind "
        f"{sorted(WAITING_KINDS)} 里。"
    )


def _tool_description_surfaces() -> list[tuple[str, str, str]]:
    """模型可见的工具描述面（三层）取样：工具级描述 + 两层字段描述。

    返回 (tool_name, field_or_self, text)。工具级描述走
    ``get_tool_description``（手写层 TOOL_PARAM_SCHEMAS 优先，回落 @tool 注册）；
    字段级覆盖手写层 properties 与 pydantic params_model 两份（双 owner 面
    都不许残留旧教学）。
    """
    from hiveweave.tools.executor import TOOL_PARAM_SCHEMAS, get_tool_description

    surfaces: list[tuple[str, str, str]] = []
    for name in sorted(_registered_tool_names()):
        surfaces.append((name, "(tool)", get_tool_description(name) or ""))
        schema = TOOL_PARAM_SCHEMAS.get(name) or {}
        for prop_name, prop in (schema.get("properties") or {}).items():
            desc = prop.get("description") if isinstance(prop, dict) else None
            if desc:
                surfaces.append((name, str(prop_name), str(desc)))
        entry = _TOOL_REGISTRY.get(name)
        model = getattr(entry, "params_model", None)
        for field_name, field in (
            getattr(model, "model_fields", {}) or {}
        ).items():
            desc = (getattr(field, "description", "") or "")
            if desc:
                surfaces.append((name, str(field_name), desc))
    return surfaces


def test_tool_descriptions_retire_commit_turn_teaching():
    """T16 三期前半绊线：auto 回合出口下 commit_turn 已从模型工具面下架，
    工具描述里「教模型调 commit_turn 收尾/挂起」的文案在说谎，必须清零。

    与 T16 二期提示词同口径改写：完成后停止输出工具调用，平台将结算；
    要等外部事件用 wait_on(refs)。合法残留（allowlist）：
      - submit_task：build_submit_task_description 的 audit_gate=True legacy
        回切分支（flag=on 专用，submit.py 归属在途批次）；
      - commit_turn 工具自身契约（legacy 模式可见时才随工具面投递）与
        spawn_subagent 的子代理收尾协议句（子代理的 commit_turn 被本地
        拦截，与回合出口模式无关）—— 后两者无 call 形态、非收尾教学，
        由下方两条精确正则天然放行。
    """
    surfaces = _tool_description_surfaces()
    assert surfaces, "工具描述面抽取退化 —— 尺子失效。"
    # ① 收尾/挂起教学句（精确定文，抓"改一半"复发）
    retired = ("People-waiting is commit_turn", "End the turn with commit_turn")
    hits = sorted(
        {
            f"{name}.{where}"
            for name, where, text in surfaces
            for phrase in retired
            if phrase in text
        }
    )
    assert not hits, (
        "工具描述残留 commit_turn 教学（auto 模式下该工具已下架，描述在说谎）：\n  "
        + "\n  ".join(hits)
        + "\n→ 改写口径：完成后停止输出工具调用，平台将结算；"
        "要等外部事件用 wait_on(refs)。"
    )
    # ② call 形态教学 `commit_turn(...)`：只允许活在 submit_task 的 legacy
    #    回切分支（flag=on 专用）。其余任何工具的字段/工具描述出现即红。
    offenders = sorted(
        {
            f"{name}.{where}"
            for name, where, text in surfaces
            if name != "submit_task" and re.search(r"commit_turn\s*\(", text)
        }
    )
    assert not offenders, (
        "工具描述残留 commit_turn(…) call 教学：\n  " + "\n  ".join(offenders)
    )


def test_contract_submit_gate_tokens_match_dispatch_schema():
    """prompts 里的 submitGate 取值 ⊆ dispatch_task schema 的枚举（owner = 工具 schema）。"""
    entry = _TOOL_REGISTRY.get("dispatch_task")
    assert entry is not None, "dispatch_task 不在注册表 —— 注册表坏了。"
    desc = entry.params_model.model_fields["submit_gate"].description or ""
    block = re.search(r"[a-z_]+(?:\s*[|+]\s*[a-z_]+)+", desc)
    assert block, (
        "dispatch_task.submit_gate 的 description 里找不到枚举块 —— schema 文案变了，先修尺子。"
    )
    gates = {t for t in re.split(r"[|+\s]+", block.group(0)) if re.fullmatch(r"[a-z_]+", t)}
    assert "module_visual" in gates and "unit" in gates, (
        f"submitGate 枚举解析退化：{sorted(gates)}"
    )
    rendered = _rendered_all()
    used: set[str] = set()
    for m in re.finditer(r"submitGate=([a-z_+|]+)", rendered):
        used.update(t for t in re.split(r"[+|]+", m.group(1)) if t)
    assert used, "渲染提示词看不到 submitGate= 点名 —— 尺子失效。"
    stale = sorted(used - gates)
    assert not stale, (
        f"prompts 点名的 submitGate 取值 {stale} 不在 dispatch schema 枚举 "
        f"{sorted(gates)} 里 —— gate 值的 owner 是工具 schema。"
    )


def test_contract_attestation_kind_enumeration_matches_policy():
    """qa_lead 的凭证 kind 枚举句 ⊆ POLICY_REQUIRED_KINDS ∪ _KIND_SOURCE_TOOL。"""
    universe = set(_KIND_SOURCE_TOOL)
    for kinds in POLICY_REQUIRED_KINDS.values():
        universe.update(kinds or ())
    assert {"test_run", "browse_e2e"} <= universe, "凭证 kind 真值集退化 —— 先修尺子。"
    rendered = _rendered_all()
    m = re.search(r"kind 跟 gate 走：([a-z_0-9 /]+)", rendered)
    assert m, (
        "qa_lead 的凭证 kind 枚举句（kind 跟 gate 走：…）找不到了 —— "
        "句子被删可删除本断言的钉扎部分，被改写请先对齐 POLICY_REQUIRED_KINDS。"
    )
    kinds = {t.strip() for t in m.group(1).split("/") if t.strip()}
    stale = sorted(kinds - universe)
    assert not stale, (
        f"qa_lead 枚举的凭证 kind {stale} 不在后端 kind 全集 {sorted(universe)} —— "
        "kind 的 owner 是 services/attestation.py POLICY_REQUIRED_KINDS。"
    )


def test_contract_misc_enum_values_match_backend():
    """零散契约值对账：verdict / evidence_kind / audit 软失败 reason / denied_by。"""
    rendered_text = _rendered_all()
    tokens = _backtick_snake_tokens(rendered_text)

    # verdict：identity 机制段 verdict="PASS"|"FAIL"，唯一权威 normalize_verdict
    verdicts = set(re.findall(r"verdict=[\"']?(PASS|FAIL)", rendered_text))
    assert verdicts == {"PASS", "FAIL"}, (
        f"prompts 的 verdict 枚举 {sorted(verdicts)} ≠ {{PASS, FAIL}}（services/tasks/verify.py）。"
    )
    for v in verdicts:
        assert normalize_verdict(v) is not None

    # evidence_kind：prompts 只教 not_applicable（权威 = delivery_contract 常量）
    ev = set(re.findall(r"evidence_kind=[\"']([a-z_]+)[\"']", rendered_text))
    assert ev == {EVIDENCE_KIND_NOT_APPLICABLE}, (
        f"prompts 的 evidence_kind 取值 {sorted(ev)} ≠ {{{EVIDENCE_KIND_NOT_APPLICABLE!r}}}。"
    )

    # audit 软失败 reason：T1 审计派单化（2026-09-29，有意变更）后 prompts
    # 不再枚举/教学（旧 executor 剧本的软失败处方已删）—— 处置处方 owner =
    # submit 回执与 SYSTEM AUDIT 任务流转文案，散文枚举必漂移。点名 = 复辟，
    # 直接红；后端 reason 宇宙自闭合由 code_audit 锚点守。
    audit_universe = set(_SOFT_FAIL_ATTEMPT_REASONS) | {"no_worktree"}
    assert audit_universe, "audit 软失败 reason 宇宙为空 —— 尺子失效，先核实。"
    audit_src = _src_text("services/code_audit.py")
    assert '"reason": "no_worktree"' in audit_src, (
        "code_audit.py 的 no_worktree 锚点消失 —— reason 宇宙变了，先修尺子。"
    )
    resurfaced = sorted(tokens & audit_universe)
    assert not resurfaced, (
        f"prompts 重新点名 audit 软失败 reason {resurfaced} —— T1 派单化后 "
        "prompts 不枚举、不教学（owner 是 submit 回执与审计任务流转文案）。"
    )

    # denied_by：DENIED_BY_SIGNATURES 的 kind 闭合于 DENIED_BY_KINDS（后端有
    # import 期断言，这里复述以防断言被拆）。prompts 目前不枚举 denied_by
    # （owner = 回执文案）；一旦出现点名，必须是**指针语境**（紧邻 回执 /
    # denied_by / 以…为准），防止散文重抄一份会漂的枚举。
    sig_kinds = {k for k, _ in DENIED_BY_SIGNATURES}
    assert sig_kinds <= DENIED_BY_KINDS, (
        f"DENIED_BY_SIGNATURES 出现未登记 kind：{sorted(sig_kinds - DENIED_BY_KINDS)}"
    )
    assert sig_kinds, "DENIED_BY_SIGNATURES 为空？—— 后端成因签名表被清空，先核实。"
    for kind in sorted(DENIED_BY_KINDS):
        for m in re.finditer(re.escape(kind), rendered_text):
            ctx = rendered_text[max(0, m.start() - 120):m.end() + 120]
            assert re.search(r"回执|denied_by|以.{0,12}为准", ctx), (
                f"prompts 出现 denied_by kind {kind!r} 的非指针式点名（散文重抄"
                "枚举）—— denied_by 的 owner 是回执文案，prompts 只许指认。上下文："
                f"{ctx!r}"
            )


# ══ 4. 通道类 ═══════════════════════════════════════════════════════

# ACK_SPARE_PREFIXES 之外的投递标记 → 后端构造点锚（测试现场验证字面量仍在；
# 后端改名/挪走 ⇒ 锚断 ⇒ 红）。锚串取**带后文的完整字面量**（裸 "[TASK]" 会
# 被任何 "[TASK XXX]" 变体骗过 —— 锚必须钉住真实构造形态）。
_CHANNEL_MARKER_SOURCES: dict[str, tuple[str, ...]] = {
    "[WAIT_TIMEOUT]": ("services/game_time.py", "agents/trigger.py"),
    "[TASK ADVANCE]": ("hooks/handlers/task_advance.py",),
    "[TURN EXIT BLOCKED]": ("services/turn_exit.py",),
    "[TASK]": ("tools/tasks/waive.py",),  # 锚走 _CHANNEL_MARKER_ANCHORS 的加长串
    "[shared fix]": ("services/failure_signature.py",),
    "[org-shape]": ("services/charter.py",),
    # T12（批 5 绊线清债）：executor 交付契约段改指真实下发头 —— 任务详情
    # 实际投递的段名是 format_submit_expectations 的 [SUBMIT CONTRACT]
    # （曾误写 [DELIVERY CONTRACT]，全后端无构造点，已修并从基线删条）。
    "[SUBMIT CONTRACT]": ("services/tasks/policy.py",),
}

# 短标记的加长锚（锚串 = 构造点真实写出的完整字面量）。
_CHANNEL_MARKER_ANCHORS: dict[str, str] = {
    "[TASK]": "[TASK] Attestation gate waived",
}

# 非投递标记：输出格式占位 / 注入段示意 —— 不是平台投递的东西。
# 只登记**渲染输出里实际出现**的（源码 docstring 里的 [str]/[dict] 等不在
# 渲染面，不预白名单 —— 出现即红，逼作者分类）。
_NON_DELIVERY_BRACKETS: frozenset[str] = frozenset({
    "[SEVERITY]",                    # inspector 剧本的输出格式占位符
    "[Earlier conversation summary]",  # grounding 段指认的压缩摘要注入标记
})

_BRACKET_MARKER_RE = re.compile(r"\[([A-Za-z][A-Za-z_ .:+-]{0,40})\]")


def _rendered_bracket_markers() -> set[str]:
    return {m.group(0) for m in _BRACKET_MARKER_RE.finditer(_rendered_all())}


def test_channel_platform_markers_have_backend_construction_points():
    """prompts 里的每个 `[XXX]` 平台标记必须 ∈ 实际投递标记集合（真值表）。

    真值表 = services/inbox.py::ACK_SPARE_PREFIXES（canonical 收件箱前缀注册表）
    ∪ _CHANNEL_MARKER_SOURCES（逐个钉到构造文件，测试现场验证字面量仍在）
    ∪ 基线 channel_unconstructed_markers（已登记的现存违规；历史上登记过
    [DELIVERY CONTRACT] —— 提示词说有这个段、后端却从未构造过它 —— T12
    修源改指真实的 [SUBMIT CONTRACT] 后已从基线删条，该出口现应为空）。
    """
    baseline = _baseline()
    unconstructed = set(baseline.get("channel_unconstructed_markers") or {})
    spare = set(ACK_SPARE_PREFIXES)
    assert "[MERGE PENDING]" in spare and "[SHIP READY]" in spare, (
        "ACK_SPARE_PREFIXES 真值表退化 —— 先修尺子。"
    )
    # 构造点锚自检：本文件声明的每个锚文件必须真含该字面量（后端改名即红）
    for marker, files in _CHANNEL_MARKER_SOURCES.items():
        anchor = _CHANNEL_MARKER_ANCHORS.get(marker, marker)
        for rel in files:
            assert anchor in _src_text(rel), (
                f"构造点锚失效：{anchor!r} 不再出现在 {rel} —— 后端改名/挪走了，"
                "请同步 _CHANNEL_MARKER_SOURCES 与 prompts 引用。"
            )
    assert ORG_SHAPE_MARKER_PREFIX == "[org-shape]"

    unknown: list[str] = []
    for marker in sorted(_rendered_bracket_markers()):
        if marker in spare or marker in _CHANNEL_MARKER_SOURCES:
            continue
        if marker in _NON_DELIVERY_BRACKETS:
            continue
        if marker in unconstructed:
            continue
        unknown.append(marker)
    assert not unknown, (
        f"prompts 出现了在后端找不到构造点的平台标记：{unknown}。\n"
        "通道描述的 owner 是投递处：① 若它真是平台标记，检查是否该进 "
        "ACK_SPARE_PREFIXES 或补 _CHANNEL_MARKER_SOURCES 锚；② 若后端根本不"
        "构造它（提示词说了一个不存在的通道形态），先改提示词；确属待修的现存"
        "违规，登记进基线 channel_unconstructed_markers（--allow-grow）并注记 "
        "owner/待办。"
    )


def test_channel_turn_exit_blocked_form_matches_backend():
    """`[TURN EXIT BLOCKED]` 字样须与后端常量形态一致（不得出现变体拼写）。"""
    assert "[TURN EXIT BLOCKED]" in _src_text("services/turn_exit.py"), (
        "turn_exit.py 的 [TURN EXIT BLOCKED] 字面量消失 —— 后端改了，同步提示词。"
    )
    assert "TURN EXIT BLOCKED — GATE UNRESOLVED" in _src_text("agents/completion.py"), (
        "completion.py 的 TURN EXIT BLOCKED — GATE UNRESOLVED 字面量消失。"
    )
    rendered = _rendered_all()
    for m in re.finditer(r"TURN EXIT BLOCKED", rendered):
        wrapped = rendered[max(0, m.start() - 1):m.end() + 1]
        assert wrapped == "[TURN EXIT BLOCKED]", (
            f"prompts 出现非规范形态 {wrapped!r}（应为 [TURN EXIT BLOCKED]，"
            "与 services/turn_exit.py 投递形态逐字符一致）。"
        )


def test_channel_shared_fix_not_described_as_error_field():
    """批 2 钉死形态：`[shared fix]` 是**独立收件箱提示**，不在工具回执 error 字段。

    缺口 3 病灶 = 「报错里的 [shared fix]」（通道说反 → agent 盯着 error 字段
    等一条永远不会进 error 的提示）。判据分两层：
      ① 旧病灶原文形态（报错里紧邻 [shared fix]）全局禁绝；
      ② 凡同段同时提到 error 字段/报错 与 [shared fix]，必须带否定
        （不在/并不/not in）—— 正确句「它**不在**工具回执的 error 字段里」放行。
    """
    assert "[shared fix]" in _src_text("services/failure_signature.py"), (
        "failure_signature.py 的 [shared fix] 构造点消失 —— 同步提示词与本测试。"
    )
    src = _prompts_source_text()
    assert not re.search(r"报错里的?\s*\[shared fix\]", src), (
        "旧病灶原文「报错里的 [shared fix]」回归 —— 通道 owner 是投递处"
        "（failure_signature 独立收件箱提示），不是工具回执 error 字段。"
    )
    for f in _prompt_source_files():
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "[shared fix]" not in line:
                continue
            mentions_error = re.search(r"error 字段|报错", line)
            if not mentions_error:
                continue
            assert re.search(r"不在|并不|不进|not\s+in", line, re.IGNORECASE), (
                f"{f.name}:{i} 提到 [shared fix] 与 error 字段/报错 的共现，但没"
                "有否定限定 —— 会被读成「提示在 error 字段里」（缺口 3 通道说反"
                "复发）。正确形态：独立收件箱提示投递 + 明写「不在 error 字段里」。"
            )


def test_channel_marker_inventory_is_not_vacuous():
    """尺子自检：通道网今天必须能看到这些已知投递标记。"""
    markers = _rendered_bracket_markers()
    for must in (
        "[BASH DONE]",
        "[MERGE PENDING]",
        "[SHIP READY]",
        "[WAIT_TIMEOUT]",
        "[TASK ADVANCE]",
        "[SUBAGENT DONE_TRUNCATED]",
    ):
        assert must in markers, (
            f"通道网看不到已知的 {must} —— 渲染或标记正则坏了，先修尺子。"
        )
    # T16 二期：prompts 不再提 legacy 的 [TURN EXIT BLOCKED]（出口提示的
    # 渠道描述禁写，owner = turn_exit.py 投递处，见
    # test_channel_turn_exit_blocked_form_matches_backend 的 owner 侧锚）。
    assert "[TURN EXIT BLOCKED]" not in markers, (
        "prompts 又出现了 [TURN EXIT BLOCKED] —— auto 语义下出口纪律只教"
        "「出口提示回来照提示继续」，渠道字样 owner 是投递处不是提示词。"
    )


# ── 基线自检与读写（family_tool_gap 机器扫描；channel 手写登记）──────


def test_baseline_file_exists_and_self_consistent():
    """基线自检：family 四族齐全、channel 登记条目带 owner/todo、无幽灵 family。"""
    data = _baseline()
    assert data, (
        f"基线 {_BASELINE_PATH.name} 不存在 —— 先执行 --write 生成"
        "（用法见本文件 docstring 基线维护节）。"
    )
    gaps = data.get("family_tool_gap") or {}
    assert set(gaps) == set(_FAMILY_ROLES), (
        f"基线 family 键 {sorted(gaps)} ≠ {sorted(_FAMILY_ROLES)} —— 手改 JSON 请同步或 --write。"
    )
    known = _registered_tool_names()
    for family, tools_ in gaps.items():
        assert isinstance(tools_, list) and all(isinstance(t, str) for t in tools_)
        ghost = sorted(set(tools_) - (known - _FAMILY_SETS[family]))
        assert not ghost, (
            f"基线 {family} 含幽灵条目 {ghost}（不是注册工具，或已在 family "
            "allowlist 里）—— 基线只登记「点名但未授权」的差集，请 --write 重新生成。"
        )
    for marker, meta in (data.get("channel_unconstructed_markers") or {}).items():
        assert isinstance(meta, dict) and meta.get("owner") and meta.get("todo"), (
            f"基线 channel 登记条目 {marker} 缺 owner/todo 注记 —— "
            "现存违规必须写明谁拥有真相与何时修。"
        )


def _baseline() -> dict:
    if not _BASELINE_PATH.exists():
        return {}
    return json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))


def _compute_family_gap(family: str, known: set[str]) -> list[str]:
    roles = _FAMILY_ROLES[family]
    role_type = "executor" if family == "executor_base" else "coordinator"
    text = "\n\n".join(_render(r, role_type) for r in roles)
    named = {t for t in _backtick_snake_tokens(text) if t in known}
    return sorted(named - _FAMILY_SETS[family])


def _write_baseline(allow_grow: bool) -> None:
    known = _registered_tool_names()
    data = _baseline()
    old_gaps: dict[str, list[str]] = data.get("family_tool_gap") or {}
    new_gaps = {f: _compute_family_gap(f, known) for f in _FAMILY_ROLES}
    bootstrap = not old_gaps  # 首次生成：尚无所冻集合，增长门不适用
    grew = {
        f: sorted(set(v) - set(old_gaps.get(f) or []))
        for f, v in new_gaps.items()
        if set(v) - set(old_gaps.get(f) or set())
    }
    if grew and not allow_grow and not bootstrap:
        raise SystemExit(
            f"family_tool_gap 有新增（拒绝生成）：{grew}\n"
            "清债（只缩不涨）用 --write；有意登记新增用 --write --allow-grow，"
            "并在提交信息里写明每个新点名是否定语境还是真矛盾。"
        )
    payload = {
        "_comment": (
            "批5 prompts 四类绊线基线（tests/test_prompt_sync_tripwires_b5.py）。"
            "family_tool_gap = 该 family 渲染提示词点名、但不在 permission allowlist "
            "的已注册工具差集：只允许缩（清掉点名/补授权后 --write）；增长 = :483 型"
            "矛盾或新的否定语境点名，须 --allow-grow 显式登记。"
            "channel_unconstructed_markers = prompts 提到、后端无构造点的标记"
            "（现存违规登记，修源后删除该条）。数值 allowlist 与非工具白名单在本文件"
            "内（带 owner 注记），不入 JSON。"
        ),
        "_regenerate": (
            "cd apps/hiveweave-py && uv run python -m "
            "tests.test_prompt_sync_tripwires_b5 --write [--allow-grow]"
        ),
        "family_tool_gap": {f: new_gaps[f] for f in sorted(new_gaps)},
        "channel_unconstructed_markers": data.get("channel_unconstructed_markers") or {},
    }
    _BASELINE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {_BASELINE_PATH} (families: {', '.join(new_gaps)})")


if __name__ == "__main__":
    if "--write" in sys.argv:
        _write_baseline(allow_grow="--allow-grow" in sys.argv)
    else:
        print("usage: python -m tests.test_prompt_sync_tripwires_b5 --write [--allow-grow]")
