"""事实位归因的**证据判据**（L3，2026-09-11）。

## 为什么需要这个模块

收口（类型强制 + 单一漏斗）只解决了「**每个出口都必须回答它属于哪一格**」，
没有解决「**答案对不对**」。现状是「我在这个分支里，所以置 runner_failed」——
这是**自我声明**，不是证据。DSH 的对应判据：

> `packages/sandbox/sandbox/src/index.ts:74-88`
> 先应用 `allowedExitCodes` → 去掉 `informationalLines`（整行等值排除）→
> 逐行匹配 `fatalSignatures`。
> **Exit status alone never proves runner failure.**

⇒ 本模块把「什么文本算 runner 失败的专属签名」声明式地列出来，**顺序**也与
DSH 一致：**先判 runner（命令从未执行），再判 denial（护栏拦住了）**。
两边都不是时**不许静默归类**，而是 fail loud —— 静默归类等于把误标从
15 处搬到 1 处，还更隐蔽。

## 归因顺序：**状态位优先，文本只配当第二层**（#15，2026-09-14）

判据来源二分（用户 2026-09-14 钦定）：

  · **状态判据**（DB 行 / 系统边界 / 权限位 / **事实位**）与措辞、语言**无关**
    ⇒ 可用；
  · **文本判据**（自由文本的子串 / 正则）随措辞与语言**整体失效**
    （换法文、换同义词即绕过）⇒ 只能兜底，不能当第一层。

本模块此前的形态正是后者：归因**先跑签名表**，把「命令有没有跑」这个**状态**
交给错误文案回答。修后的阶梯（**顺序即判据**，前两层判不出来才向下走）：

  1. **布尔位**（构造点知道的事实）：``dialect_failed`` → ``runner_failed``；
     ``runner_failed`` → ``runner_failed``；``command_failed`` → ``command_failed``。
     位名与顺序**逐字同源**于 ``services/failure_signature.py::attribution_of``，
     两边共用 :func:`fact_from_bits` 单一实现 —— 不许各自演化。
  2. **文本签名表**（`RUNNER_FAILURE_SIGNATURES` / `BAD_ARGS_SIGNATURES`）：
     只在**位缺失**时才参考；它是**兜底观测**，不是判据本体。
  3. **代码作用域**：``timeout_kind == "wait"``（审批窗口等待）。
  4. 三者都不命中 ⇒ **不猜**，落 ``outcome_unknown``（语义＝「结果未知」）。

⚠ 第 4 步**不得**落 ``runner_failed``：后者语义是「命令从未执行」⇒ 下游读成
「无副作用、可直接重试」，而本模块的触发点在**执行之后**（executor 的
normalize 尾）⇒ 可能诱发**副作用双发**（见 :func:`finalize_tool_result` 的
长注释与 ``tests/test_p0_3_orphan_root_cause.py``）。归不到证据时，保守方向
是「结果未知、别盲目重试」，而不是「放心重试」。

## 与 L6 的分工

`bad_args` 不是「runner 失败的一种」，是**调用方责任**：模型把路径/端口写错，
改参数就能过。判据是「**这个错误是否随调用方参数变化**」——
故它排在 runner 签名**之后**（runner 签名命中即命令根本没跑起来），
但在 timeout/denial 之前（写错路径时护栏根本没参与）。

## 为什么签名表而不用分支内联

分支内联 = 约束散在 28 个构造点里，就是本批要消灭的形态。声明式表的
另一个好处：`tests/test_fact_positions_coverage.py` 能把「表里的每一条」
与「真实错误文本」对起来，而内联的 if 无法被机械枚举。
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Literal, NamedTuple

import structlog

from hiveweave.tools.result import FactKind
from hiveweave.util.redact import redact_secrets

log = structlog.get_logger(__name__)

#: runner 失败的**专属签名**（命令从未执行）。
#:
#: 每条都对应一个已被实测观察到的出口（见各条注释的出处）。
#: 匹配是**大小写不敏感的子串**匹配（对齐 DSH 的 `fatalSignatures` 语义）。
#:
#: ⚠ **E19 收窄（2026-09-16，实测驱动）**：本表跑的是**归一化后的子串**匹配，
#: 而匹配对象是**含命令 stdout/stderr 的整段回执** ⇒ 一旦 needle 是常见英文词，
#: 它就会命中**命令的输出内容**，把「命令跑了但失败」误报成「命令从未执行」。
#: 用 58/59 的真实数据在**有位可判**的行上量（位 = 地面真值）：
#:   文本层判 `runner_failed` 的 35 行（58）/ 40 行（59）里，
#:   与位（`command_failed`）**冲突 12 行（34%）/ 24 行（60%）**。
#: ⚠ **口径（审计 D1）**：这是**纯函数度量**，不是 36 次生产误标 ——
#: 那些行都带 `command_failed` 位而 `blocked=False`，而文本表**只在 blocked
#: 分支被咨询** ⇒ 生产上它们走的是位、根本没问文本。所以准确的说法是
#: 「文本层**若被咨询**会误报 36 次」。收窄它仍然必要：文本表正是**位缺失**
#: 时（`blocked=False` + 无位 + 有 error，走 `assert_fact_complete` 兜底那条）
#: 唯一可能被用到的判据源，而那些行恰恰没有地面真值可查。
#:   冲突行命中的 needle 只有 4 个：`"spawn"` ×25、`"approval"` ×6、
#:   `"permission"` ×4、`"does not exist"` ×1。它们的现场是命令输出里的
#:   `post-approval cleanup`、`Permission denied`、`fatal: cannot create directory` 等
#:   ——**全是内容，不是平台护栏文案**。
#: ⇒ 这 4 条按「是否只可能出现在**平台自产**文案里」重写（其余条目实测零冲突，
#:   本批不动 —— 不做没有数据的"顺手清理"）。
RUNNER_FAILURE_SIGNATURES: tuple[str, ...] = (
    # ── B 组：命令安全 / 封印护栏（bash.py execute_bash / run_command）──
    "command blocked",                     # 自毁/敏感路径/.hiveweave 系统目录
    "system-level destructive command",
    "cannot access .hiveweave system directory",
    "拒绝执行",                             # eval_seal 封印工作区（中文文案）
    # ── C 组：沙箱 / cwd / 平台前提 ──
    "sandbox violation",
    "cwd must stay inside workspace",
    "working directory does not exist",
    "沙箱不可用",                           # SandboxUnavailableError（fail-closed）
    # ── D 组：方言 gate（命令没跑）──
    "not available in this shell",
    # ⚠ E19 审计 D2：原表此处有 `"not recognized as"`，**删** —— 实测它在
    # 58/59 命中的 8 段文本**全部**同时命中上面那条（边际覆盖 = 0），
    # 而它出现在平台 gate 文案的**括号说明**里（`bash.py` 的
    # `running them yields "not recognized as ... cmdlet"`），不是任何出口的主判据。
    # ⚠ E19：原表此处有裸 `"does not exist"` 与裸 `"dialect"`。
    #   · `"does not exist"` **删**：cwd 类平台文案由上面那条更具体的
    #     `"working directory does not exist"` 覆盖；留着只会命中命令输出
    #     （实测现场：`fatal: cannot create directory at ...` 附近）。
    #   · `"dialect"` **删**：平台**错误文案**里根本没有这个词（它只出现在
    #     日志事件名与 docstring 里），即"零验证覆盖 + 潜在误命中"。
    # ── E 组：spawn / runner 自身故障 ──
    "no tool executor",
    "[no tool executor]",
    # ⚠ E19：原为裸 `"spawn"`（实测在**有位可判**的行上冲突 25 次）。
    # 收窄到**平台自己的两种措辞**（`process_registry.py:989` 的
    # `Failed to spawn: …` 与 `bash.py:1792` 的 `Failed to spawn shell: …`）。
    # ⚠ 残余（如实登记）：命令自己的输出若恰好含这两串（如 npm 的
    # `Failed to spawn child process` 不命中，但含 `Failed to spawn:` 的会）
    # 仍会被判成 runner —— 这是**文本层当兜底**的固有上限，不靠继续堆词解决。
    "failed to spawn:",
    "failed to spawn shell",
    "failed to start",                     # `dev_server_tools.py:410/434`（实测零冲突）
    "cannot find the path",
    # ── A 组：审批通道 / 权限拒绝（从未派发）──
    "approval_channel_unavailable",        # ⚠ E19：原为裸 `"approval"`（实测冲突 6×，
                                           #    现场是命令输出里的 `post-approval cleanup`）
    "审批",                                 # 审批通道超时的中文文案（平台自产）
    # ⚠ E19：原表有裸 `"permission"`，**换成平台自己的两种措辞**（实测冲突 4×，
    # 现场是命令输出里的 `Permission denied` / `Access is denied` —— 那是**业务失败**，
    # 命令跑了）。下面两条逐字来自平台源码，不是猜的：
    #   · `executor.py:3355` / `pipeline.py:421` 的 `f"Permission rejected: {exc}"`
    #   · `executor.py:3284` / `pipeline.py:336` 的 `f"Error: Permission check failed: {exc}"`
    # ⚠ 刻意**不**收 `"permission denied:"`：那是 `org_tools.py:1588` 的平台文案，
    # 但也正是**命令 stderr 的高频措辞**（`Permission denied: /path`）⇒ 收它会立刻
    # 把 E19 的老毛病带回来。代价是那一条出口只能靠**位**兜（审计 C3 订正：
    # `pipeline.py:336` 的 `ToolResult.err(...)` **确实不带 fact**，
    # 所以我原先写的"那几条路径本来就显式置了 fact"只对
    # `executor.py:3284/3355`、`pipeline.py:421` 成立，不是全部）——
    # 这条取舍留在「残余」，不在本批扩表。
    "permission rejected:",
    "permission check failed:",
)

#: 调用方参数错的专属签名（L6）。
#:
#: 判据：**随调用方参数变化** —— 模型换个路径/端口就能过，平台无责。
#: 注意这里**不含**泛化的 "not found"：那可能是 runner 侧前提缺失
#: （cwd 不存在），而 cwd 不存在是 `runner_failed`（见上表）。两者
#: 的区分靠**具体措辞**，这正是声明式签名表必须逐条列出而不能用
#: 通配的原因。
BAD_ARGS_SIGNATURES: tuple[str, ...] = (
    "疑似重复 worktree 前缀路径",
    "duplicate worktree prefix",
    "出界",                                 # 路径越界类参数错
)
# ⚠ 原表里有一条裸 `"port"`（dev-server 保留端口），已**删除**：本表的匹配是
# 子串匹配 ⇒ `"port" in "ImportError" / "unsupported" / "important" / "report"`
# 全部命中 ⇒ 任何含 import/支持/report 的错误都被归成「调用方参数错」，把 agent
# 指向改参数这条错路（实测：`ImportError: cannot import name 'x'` → bad_args）。
# 该条改为**词边界**判据，见下方 `_SIGNATURE_ORDER` 的 `regex` 条目 ——
# 保留端口文案（`Port 4000 is reserved …` / `not --port 4000.`）仍命中。

#: 一条签名判据。
#:
#: ``kind="substr"`` 是**归一化后的子串**匹配（对齐 DSH 的 `fatalSignatures`
#: 语义，也是本表历史语义）；``kind="regex"`` 是 ``re.search``（用于需要
#: **词边界/数字边界**的形态 —— 子串匹配会误命中，见上面 `"port"` 的反例）。
#: 两种都跑在**归一化文本**上（小写 + 单空格）。
class _Sig(NamedTuple):
    kind: Literal["substr", "regex"]
    value: str


def _substr(*needles: str) -> tuple[_Sig, ...]:
    """把「子串型签名表」转成判据条目（保持表的可读性，见上两张 `_SIGNATURES`）。"""
    return tuple(_Sig("substr", n) for n in needles)


#: `fact` 与签名的对应表（顺序即判据顺序，对齐 DSH「先 runner 再 denial」）。
#:
#: 顺序不可随意调换：`bad_args` 的签名较宽（词边界的 `port` 也会命中
#: 「端口保留导致的 spawn 失败」文案里的 port），若排在 runner 签名前面，
#: 会把那类 runner 故障误判成参数错。
_SIGNATURE_ORDER: tuple[tuple[FactKind, tuple[_Sig, ...]], ...] = (
    ("runner_failed", _substr(*RUNNER_FAILURE_SIGNATURES)),
    ("bad_args", _substr(*BAD_ARGS_SIGNATURES) + (_Sig("regex", r"\bport\b"),)),
)

_NORM_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """归一化到小写 + 单空格（签名匹配对换行/多空格不敏感）。"""
    return _NORM_RE.sub(" ", (text or "")).strip().lower()


# ── 第一层判据：布尔位（状态）────────────────────────────────────
#
# 位名**只此三个**（与 `services/failure_signature.py::attribution_of` 的读取
# 集合逐字一致；不许在此之外发明位名，也不许让两边各自演化）。
_DIALECT_BIT = "dialect_failed"
_RUNNER_BIT = "runner_failed"
_COMMAND_BIT = "command_failed"
#: 落样本时要一并记录「当时有哪些位」（含 `blocked` —— 它不是归因位，
#: 但「blocked=True 却没有任何归因位」正是我们要看见的形态）。
_SAMPLE_BITS: tuple[str, ...] = (
    _DIALECT_BIT, _RUNNER_BIT, _COMMAND_BIT, "blocked",
)

#: 位 → 事实位（顺序即优先级；`dialect_failed` 语义 = 方言不兼容 ⇒ 命令从未执行）。
_BIT_FACTS: tuple[tuple[str, FactKind], ...] = (
    (_DIALECT_BIT, "runner_failed"),
    (_RUNNER_BIT, "runner_failed"),
    (_COMMAND_BIT, "command_failed"),
)


def _bits_mapping(source: dict | object) -> dict[str, object]:
    """把结果 dict / `ToolResult` 归一成**位视图**（只读，不造 KeyError）。

    对 `ToolResult`：取 `extra`（裸位经 `to_dict` 会落在这里）+ 字段位
    ``blocked``（它不是 extra，但样本里必须能看出「当时是护栏拒绝」）。

    ⚠ **不读** `runner_failed`/`command_failed` **派生属性**：它们不是「声明的
    位」，而是 `fact` 的视图 —— 在 fact 还是占位值时它们恒 `False`，读进来
    会把「没有位」谎报成「位存在且为假」（实测踩过：样本的 `bits_present`
    里凭空出现两个 False 位）。
    """
    if isinstance(source, dict):
        return source
    mapping: dict[str, object] = {}
    extra = getattr(source, "extra", None)
    if isinstance(extra, dict):
        mapping.update(extra)
    blocked = getattr(source, "blocked", None)
    if blocked is not None:
        mapping.setdefault("blocked", blocked)
    return mapping


def state_bits(source: dict | object) -> dict[str, bool]:
    """读出**存在的**归因状态位（缺位**不补** `False` —— 「没说」≠「说了否」）。

    只含 :data:`_BIT_FACTS` 里的三个位；值统一成 `bool`（位可能是 `None`/真值）。
    """
    mapping = _bits_mapping(source)
    bits: dict[str, bool] = {}
    for bit, _fact in _BIT_FACTS:
        value = mapping.get(bit)
        if value is not None:
            bits[bit] = bool(value)
    return bits


def fact_from_bits(source: dict | object) -> FactKind | None:
    """**第一层判据**：状态位 → 事实位；位缺失返回 `None`（**不猜**）。

    与 ``services/failure_signature.py::attribution_of`` **同一判据**（顺序：
    ``dialect_failed`` > ``runner_failed`` > ``command_failed``）—— 该函数改为
    调用本函数，避免「一处改了另一处没改」的经典复发（本仓纪律：同一判据只有
    一份实现）。
    """
    bits = state_bits(source)
    for bit, fact in _BIT_FACTS:
        if bits.get(bit):
            return fact
    return None


# ── P0-3：沙箱/ACL 拒绝的**成因细分**（DeniedBy）────────────────
#
# 层次与既有一致：**先状态、后证据、不足不猜**。
#   1. 状态层 `is_acl_rejection`：非零退出 + 拒绝方言命中 ⇒ 才算「一次拒绝」；
#      否则返回 None（不是拒绝，别硬贴成因）。
#   2. 证据层 `extract_denied_paths`：OS 只给文本，被拒**路径**是唯一的路径证据。
#      与 `boundary_root` 比（normalize 后）：
#        · 命中**显式传入的封条集合** ⇒ `sealed_git`
#        · 全部在授权树**外**            ⇒ `outside_boundary`
#        · 全部在授权树**内**            ⇒ `no_write_sid`
#          （树内的 `.hiveweave` 等 PROTECTED 面正是这一类 —— 实测 19/33 假越界全在这）
#        · 内外**混着**、或抽不到路径    ⇒ `unknown_acl`（不猜）
#   3. 本函数只细分**处方**；事实位仍落 `runner_failed`（归因归属不变）。
#
# ⚠ 这不是「用文案判意图」：方言与路径是**操作系统的拒绝证据**，不是 agent 的措辞；
#    与 `classify_error_text` 同族但更窄（只认拒绝方言 + 路径），且**证据不足时
#    显式返回 unknown_acl**，绝不默认归到某一格。
def _rejection_dialect() -> tuple[str, ...]:
    """拒绝方言的**唯一源**在 `services/acl_sandbox/service.py::REJECTION_DIALECT`
    （对外公开面 + 被 `test_acl_sandbox_dialect` pin 住）。

    惰性导入：`acl_sandbox` 包 `__init__` 会拉 `service.py`，模块级导入有环风险。
    兜底同值 + 由测试钉住「兜底 == 唯一源」⇒ 兜底不会静默漂移。
    """
    try:
        from hiveweave.services.acl_sandbox.service import REJECTION_DIALECT

        return tuple(REJECTION_DIALECT)
    except Exception:  # noqa: BLE001 — 导入环/未装：退回同值，绝不变成「不认方言」
        return ("Access is denied", "Access to the path", "Permission denied")
_DENIED_PATH_RE = re.compile(
    # ⚠ 两个分支都**限制在同一行内**：跨行会让 `Access is denied.\n+ Copy-Item 'D:\\x'`
    # 这类「拒绝与另一条无关语句」被拼成「'D:\x' 被拒」⇒ 在**零证据**下断言越界
    # （审计 A1②：那正是本条要治的病，不能自己再犯一次）。
    r"(?:path|文件|路径)\s*['\"“](?P<q1>[^'\"”\n]+)['\"”]\s*(?:的访问被拒绝|is denied)"
    r"|(?:Access to the path|Access is denied)[^'\"“\n]*?['\"“](?P<q2>[^'\"”\n]+)['\"”]",
    re.IGNORECASE,
)


def is_acl_rejection(stderr: str, exit_code: object) -> bool:
    """状态层：非零退出 + 拒绝方言命中 = 一次沙箱/ACL 拒绝（`None`/0 都不算）。"""
    try:
        if exit_code is None or int(exit_code) == 0:
            return False
    except (TypeError, ValueError):
        return False
    norm = _normalize(stderr)
    if not norm:
        return False
    return any(d.lower() in norm for d in _rejection_dialect())


def extract_denied_paths(stderr: str) -> list[str]:
    """抽出被拒路径（单/双引号、中英两种形态）；抽不到返回 `[]`（不猜）。"""
    out: list[str] = []
    for m in _DENIED_PATH_RE.finditer(stderr or ""):
        for key in ("q1", "q2"):
            v = m.groupdict().get(key)
            if v and v.strip() and v not in out:
                out.append(v.strip())
    return out


def _norm_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


#: 封条函数（`service.py::_seal_git_bootstrap_files`）产出的戳前缀。
#: ⚠ 只剥**已知前缀**（不能 `split(":", 1)`：裸 Windows 路径 `D:\x` 会被砍成 `\x`，
#: 于是"裸路径也能传"的承诺变成谎报 —— 审计 A2）。
_SEAL_STAMP_PREFIXES: tuple[str, ...] = (
    "create+seal:",
    "seal:",
    "deny-dc-all:",
)


def _sealed_pairs(sealed) -> list[tuple[str, str]]:
    """`sealed` → `[(原始路径, 归一化路径)]`。

    可传裸路径，也可传封条戳串（`seal:<p>` / `create+seal:<p>` / `deny-dc-all:<p>`）。
    保留原始形态是为了 `sealed_by` 能**如实记录**封条函数说的话（而不是重推一遍）。
    """
    out: list[tuple[str, str]] = []
    for item in sealed or ():
        raw = str(item)
        for pref in _SEAL_STAMP_PREFIXES:
            if raw.startswith(pref):
                raw = raw[len(pref):]
                break
        out.append((raw, _norm_path(raw)))
    return out


def _sealed_targets(sealed) -> list[str]:
    """仅归一化形态（内部/测试用）。"""
    return [norm for _raw, norm in _sealed_pairs(sealed)]


def sealed_match(stderr: str, sealed) -> str | None:
    """被拒路径命中封条集合时返回**该封条目标路径**（已剥戳前缀），否则 `None`。

    单一匹配实现：`classify_denied_by` 的 `sealed_git` 分支与 `sealed_by` 的取值
    都用它 —— 两处各写一遍必然漂移。
    """
    pairs = _sealed_pairs(sealed)
    if not pairs:
        return None
    for denied in extract_denied_paths(stderr):
        dn = _norm_path(denied)
        for raw, norm in pairs:
            if dn == norm or dn.startswith(norm + os.sep):
                return raw
    return None


def classify_denied_by(
    stderr: str,
    exit_code: object,
    *,
    boundary_root: str | None = None,
    sealed=None,
) -> str | None:
    """把一次沙箱拒绝细分成 `DeniedBy` 之一。

    返回 ``None`` = **不是**沙箱/ACL 拒绝（调用方不要追加沙箱提示）。
    返回 ``"unknown_acl"`` = 是拒绝，但证据不足以细分（**不许猜**）。

    ⚠ Stage 边界（2026-09-21）：`sealed_git` 目前**只在显式传入 `sealed`** 时判定；
    「封条返回值 → 执行阶段」的携带（`sealed_by` 位）是下一阶段，尚未接线。
    """
    if not is_acl_rejection(stderr, exit_code):
        return None
    paths = extract_denied_paths(stderr)
    if not paths:
        return "unknown_acl"

    if sealed_match(stderr, sealed) is not None:
        return "sealed_git"
    root = _norm_path(boundary_root).rstrip(os.sep) if boundary_root else ""
    # ⚠ 去掉尾分隔符：`normpath("D:\\") == "D:\\"` ⇒ `root + os.sep` 永不匹配，
    #   盘符根下的一切都会被判 out（审计 A5）。
    sides: set[str] = set()
    for raw in paths:
        pn = _norm_path(raw)
        # ⚠ 相对路径无从比边界（审计 A1①：`'src\a.txt'` 曾被判 outside）⇒ 计入 "?"
        if not root or not os.path.isabs(raw):
            sides.add("?")
        elif pn == root or pn.startswith(root + os.sep):
            sides.add("in")
        else:
            sides.add("out")

    if sides == {"out"}:
        return "outside_boundary"
    if sides == {"in"}:
        return "no_write_sid"
    return "unknown_acl"


def classify_error_text(error: str) -> FactKind | None:
    """**第二层判据**：按签名表归类错误的成因；**无签名命中时返回 None**（不猜）。

    调用方拿到 None 必须显式处理（fail loud / 保留未确定），不得默认
    归到某一格 —— 那正是「无证据归类」。

    ⚠ 本函数只该在**布尔位缺失**时被调用（:func:`fact_from_bits` 先跑）：
    文本判据随措辞与语言整体失效，把它当第一层等于把归因交给文案。
    """
    norm = _normalize(error)
    if not norm:
        return None
    for fact, signatures in _SIGNATURE_ORDER:
        for sig in signatures:
            if sig.kind == "substr":
                hit = _normalize(sig.value) in norm
            else:  # regex：词边界类（见 `_SIGNATURE_ORDER` 的 `\bport\b`）
                hit = re.search(sig.value, norm) is not None
            if hit:
                return fact
    return None


def classify_blocked_fact(
    tool_name: str,
    error: str,
    *,
    timeout_kind: str | None = None,
    bits: dict | object | None = None,
) -> FactKind:
    """`blocked` 结果的事实位归因 —— 单一判据入口（**位优先**）。

    阶梯（#15，2026-09-14；顺序即判据）：
    1. **布尔位**（`dialect_failed` > `runner_failed` > `command_failed`）——
       状态判据，与措辞/语言无关，故排第一；
    2. 文本签名表（`classify_error_text`）：位缺失才参考，先 runner 再 bad_args
       （顺序对齐 DSH `packages/sandbox/sandbox/src/index.ts:109-115`）；
    3. 审批等待（`timeout_kind == "wait"`）：平台侧流程阻塞 ⇒ runner；
    4. 全不命中 ⇒ **AssertionError**（fail loud，绝不静默归类）。

    ``bits`` 是位视图（结果 dict 或 `ToolResult` 均可，缺省 `None` = 没有位）。
    本函数保持**严格**（判不出来就抛）—— 运行时的 fail-soft 兜底在
    :func:`finalize_tool_result`，两者分工见那里的长注释。

    ``timeout_kind == "wait"`` 属平台侧（审批窗口等待），语义是「命令从未
    派发」，故归 runner_failed —— 这是**代码作用域归属**，不靠文案匹配
    （对齐 DSH `packages/guard/timeout-policy/src/index.ts:69-73` 的
    「a nested outer deadline reads as undefined here」同款思路）。
    """
    bit_fact = fact_from_bits(bits) if bits is not None else None
    if bit_fact is not None:
        return bit_fact
    kind = classify_error_text(error)
    if kind is not None:
        return kind
    if timeout_kind == "wait":
        return "runner_failed"
    raise AssertionError(
        f"blocked tool result has no fact evidence: tool={tool_name!r} "
        f"error={error!r:.200} — 布尔位与签名表都没命中时不得静默归类；"
        f"要么让构造点声明布尔位/fact，要么补 RUNNER_FAILURE_SIGNATURES/"
        f"BAD_ARGS_SIGNATURES（见 fixplan 批次 2 §1.4c / #15）"
    )


# ── 判不出来时的 fail-loud 样本（#15 修法 3）──────────────────────
#
# 「降级静默」才是病灶：归因退化成 outcome_unknown 时没人知道，直到某天下游的
# stall/重试分流全失灵。故留一条**带原始文案**的样本 ⇒ ①观测「判不出来」的真实
# 比例（这是**平台可控**的指标）；②拿到真实文案后**在判据层重建**，而不是靠人
# 猜上游会怎么写。
#
# 落库范式镜像 `llm/unknown_error_samples.py`（同一套「同步记样本 + 调用方落库」
# 分层）：本函数是**同步**的且**拿不到 agent_id**（agent_id 是调用方作用域的信息）
# ⇒ 不为记录而给它新增参数、也不猜 id：就地 log + 把 payload 放进结果
# （`out["unclassified_sample"]`），由拿得到 agent_id 的调用方落 agent_events。
UNCLASSIFIED_SAMPLE_EVENT = "fact_position_unclassified_sample"
#: 样本里保留的文案长度（够了：判据特征词都在前 200 字内）。
_SAMPLE_PREVIEW = 200


def note_unclassified_sample(
    *,
    tool: str,
    error: str = "",
    status: object = None,
    bits: dict | object | None = None,
) -> dict:
    """记一条「布尔位缺失 + 文本未命中」的样本，返回可落库 payload。

    payload 形状：``{tool, error_preview(≤200 字), status, bits_present}``；
    ``bits_present`` 是 ``{位名: bool}`` —— **带值**，只记名字会把「构造点显式
    声明为 False」读成「位已置真」，而 True/False 恰恰是判据的分叉点（同一个坑
    见 :func:`_bits_mapping` 的 docstring）。

    ⚠ 本函数**必须永不抛异常**：它挂在归因路径上，抛异常会把一次「判不出来」
    升级成「整条工具调用炸掉」。
    ⚠ **脱敏是硬要求**：任何情况都不得把 key/token 写进样本 —— 复用
    `util/redact.py` 的唯一实现（不新增第 N 份密钥匹配面）。
    """
    try:
        mapping = _bits_mapping(bits) if bits is not None else {}
        payload: dict = {
            "tool": tool,
            "error_preview": redact_secrets(str(error or ""))[:_SAMPLE_PREVIEW],
            "status": status,
            # 名字 + 值：只记名字是**半个判据** —— 「构造点显式说了 False」与
            # 「说了 True」在归因上指向完全相反的结论，而两者都会出现在这个
            # 列表里（`state_bits` 的语义是「位存在」≠「位为真」）。
            "bits_present": {
                k: bool(mapping[k])
                for k in sorted(_SAMPLE_BITS)
                if mapping.get(k) is not None
            },
        }
        log.error(
            UNCLASSIFIED_SAMPLE_EVENT,
            action=(
                "事实位归因判不出来（位缺失 + 文本未命中）⇒ 落 outcome_unknown。"
                "**别急着往签名表补词**：先看这条文案属于哪类，优先把判据改成"
                "状态判据（让构造点声明布尔位 / fact）"
            ),
            **payload,
        )
        return payload
    except Exception as e:  # noqa: BLE001 — 见 docstring：绝不抛
        log.debug("unclassified_sample_note_failed", error=str(e)[:200])
        return {}


#: F2 声明支路降采样分母（``HIVEWEAVE_FACT_SAMPLE_DECLARED``）：非冲突的
#: 声明失败按 1/N 产样本，证明观测通道是活的。``1`` = 恒采样、``0`` = 关、
#: 默认 20。⚠ 判据是**哈希取模**不是 ``random`` —— 守卫与事后取证必须
#: 可复现（同一输入两次调用结果一致）。
_DECLARED_SAMPLE_ENV = "HIVEWEAVE_FACT_SAMPLE_DECLARED"
_DECLARED_SAMPLE_DEFAULT_N = 20


def _declared_sample_due(tool: str, declared_fact: str, error: str) -> bool:
    """确定性降采样判定：``sha1(tool|fact|error 前 120 字) % N == 0``。

    哈希输入**不含时间戳/随机数** ⇒ 同一失败重复撞到时采样决策一致；
    错误原文参与哈希 ⇒ 不同错误错开落点，避免永远只采到同一条。
    """
    raw = os.environ.get(_DECLARED_SAMPLE_ENV, "")
    try:
        n = int(raw) if raw.strip() else _DECLARED_SAMPLE_DEFAULT_N
    except ValueError:
        n = _DECLARED_SAMPLE_DEFAULT_N
    if n <= 0:
        return False
    digest = hashlib.sha1(
        f"{tool}|{declared_fact}|{error[:120]}".encode("utf-8")
    ).hexdigest()
    return int(digest, 16) % n == 0


def note_declared_sample(
    *,
    tool: str,
    declared: str,
    kind: str,
    bits_fact: str | None = None,
    error: str = "",
    status: object = None,
    bits: dict | object | None = None,
) -> dict:
    """记一条「构造点已声明 fact 的失败」样本（F2，**只观测、不改归因**）。

    与 :func:`note_unclassified_sample` 走**同一落库通道**
    （``out["unclassified_sample"]`` → ``executor._emit_unclassified_sample``
    → ``agent_events``，event_type 同为 ``fact_position_unclassified_sample``），
    payload 用 ``kind`` 区分来源：``conflict`` = 声明与布尔位打架（**必产**），
    ``declared_sampled`` = 非冲突降采样（1/N，通道活性证据）。

    ⚠ **绝不打 ERROR 日志**（§3.5 最大风险）：shell 家族的声明失败是常态
    流量（58-61 四轮 164 行），无条件 ERROR 会把日志炸掉；冲突场景另有
    ``fact_position_declared_conflicts_with_bits`` WARNING 快信号，本函数
    只落 state 证据。
    ⚠ 本函数**必须永不抛异常**（挂在归因路径上）；**同样脱敏**。
    """
    try:
        mapping = _bits_mapping(bits) if bits is not None else {}
        payload: dict = {
            "kind": kind,
            "declared": declared,
            "bits_fact": bits_fact,
            "tool": tool,
            "error_preview": redact_secrets(str(error or ""))[:_SAMPLE_PREVIEW],
            "status": status,
            "bits_present": {
                k: bool(mapping[k])
                for k in sorted(_SAMPLE_BITS)
                if mapping.get(k) is not None
            },
        }
        log.debug("fact_position_declared_sample", **payload)
        return payload
    except Exception as e:  # noqa: BLE001 — 见 docstring：绝不抛
        log.debug("declared_sample_note_failed", error=str(e)[:200])
        return {}


def assert_fact_complete(tool_name: str, result: dict) -> None:
    """启动/收口断言：shell 类失败结果**必须**带可用事实位。

    对「成功」「blocked=False 且无 error」放行；其余必须在四格里有答案。
    """
    if result.get("success"):
        return
    if result.get("fact") is None:
        raise AssertionError(
            f"shell tool {tool_name!r} failed without a fact position: "
            f"error={result.get('error')!r:.200} — "
            f"见 fixplan 批次 2 §1.4a（blocked 必须声明 fact）"
        )
    fact = result["fact"]
    from hiveweave.tools.result import FACT_KINDS

    if fact not in FACT_KINDS:
        raise AssertionError(
            f"shell tool {tool_name!r} declared unknown FactKind {fact!r}"
        )


#: 需要事实位收口的工具（shell 家族）。判据：这些工具的错误文本里
#: 「命令没跑起来」与「命令跑了没过」是**可区分的**，且下游 stall 归因
#: 消费该区分（`llm/streamer/doom_loop.py` / `advisory.py`）。
SHELL_SECURITY_LEVEL_TOOLS: frozenset[str] = frozenset({
    "bash", "pwsh", "run_command", "execute_bash",
    "start_dev_server", "lookup_dev_server",
})


def finalize_tool_result(
    tool_name: str,
    raw: dict | object,
) -> dict:
    """**唯一收口**：把工具返回归一为契约 dict，并保证事实位完整。

    ⚠️ **不能只挂在 `_emit_tool_execute_after`**（`executor.py:2446`）：
    它的 docstring 明写「Pre-execution failures (args/permission/ask) never
    emit」——而 28 处事实位构造点里 **17 处（审批 + 护栏）正是 pre-execution
    失败**，恰好全在它覆盖之外。故本函数必须在**两条执行器各自的
    normalize 尾**都被调用。

    **归因阶梯（#15，2026-09-14；顺序即判据）**：结果
    **失败**且**未声明 fact** 时补位，顺序＝
    **布尔位 → 文本签名表 → 代码作用域（`timeout_kind=="wait"`）→ 不猜
    （`outcome_unknown`）**；位缺失且文本未命中时另落一条 fail-loud 样本
    （见 :func:`note_unclassified_sample`，payload 同时进
    `out["unclassified_sample"]` 供调用方落库）。

    ⚠ **E21（2026-09-16）：原来的 `judge_blocked: bool = True` 开关已删除**。
    它是个**潜式 opt-out**：任何调用方传 False 就能静默跳过整个归因阶梯，
    而**没有任何一处会因此报警**（守卫只看"该出口声明的 fact 是否合法"，
    不看"它有没有走归因"）。全仓核查过：**没有任何调用方传过 False**
    （`grep -rn "judge_blocked=" ` 只命中定义与文档）⇒ 它是一枚**休眠的
    旁路**，删掉零影响、留着是隐患。约束应当"住在做那件事的操作内部"，
    而不是做成一个可被调用方悄悄关掉的参数。
    """
    from hiveweave.tools.result import ToolResult, finalize_fact_dict

    # E23：分母（本收口判过多少次）—— 与样本分子配对才构成"比例"。
    try:
        from hiveweave.llm.unknown_error_samples import note_judgement

        note_judgement("fact_position")
    except Exception:  # noqa: BLE001 — 计数绝不打断收口
        # 空 handler 是 L17 事故的温床（静默失效只在**下游**炸开）⇒ 留痕。
        log.debug("fact_positions.note_judgement_failed")

    unclassified: dict = {}
    if isinstance(raw, ToolResult):
        r = raw
        declared = r.fact is not None
    elif isinstance(raw, dict):
        # blocked 必须显式透传：进 extra 会被 ToolResult 字段恒胜覆盖抹掉
        # （审计 P2，潜伏陷阱）。
        _declared = raw.get("fact") or None  # "" 视为未声明（免得构造期报 unknown）
        declared = _declared is not None
        _blocked = bool(raw.get("blocked"))
        # ⚠ blocked=True 且未声明 fact 时，`ToolResult.__post_init__` 的不变式会
        # **硬抛** ValueError（`blocked result must declare its fact kind`，实测
        # 2026-09-14）—— 而本函数在 dispatch 的 try/except 之外被调用 ⇒ 未捕获
        # 异常会炸掉整条工具调用（与下方 assert_fact_complete 的 P0-3 同构，
        # 只是炸点更靠前）。故先用一个**与兜底同格**的保守占位
        # （`outcome_unknown`）过不变式，随后由真实归因覆盖；两者同格 ⇒
        # 归因全不命中时也不会泄漏出比兜底更乐观的语义（尤其**不会**变成
        # 「命令从未执行」那种"放心重试"信号）。
        r = ToolResult(
            success=raw.get("success", True),
            output=raw.get("output", ""),
            error=raw.get("error"),
            blocked=_blocked,
            fact=_declared or ("outcome_unknown" if _blocked else None),
            extra={
                k: v
                for k, v in raw.items()
                if k not in ("success", "output", "error", "blocked", "fact")
            },
        )
    else:
        return ToolResult.ok(str(raw)).to_dict()

    # 事实位归因：失败且**未声明 fact** 时按阶梯补位（#15：**位优先**）。
    #
    # ⚠️ 判不出来时**不能把 AssertionError 抛到运行时**（独立审计 P1，2026-09-11）：
    # 本函数在 executor 的第 4 步、**dispatch 的 try/except 之外**被调用
    # （`executor.py:3112`），未捕获异常会**直接炸掉这次工具调用**，
    # agent 拿到的是「平台崩了」而不是「这个工具被拦住了」——
    # 这比"归错格"更糟。反例：`file.py` 的 `"Unknown error"` 不命中任何签名。
    #
    # 故此处 **fail loud 但不 fail hard**：判据失配是**平台侧需修**的信号，
    # 记 ERROR 日志 + 落样本（CI/审计可捞），运行时归到 `outcome_unknown`。
    # ⚠ 兜底**不得**是 `runner_failed`：语义 =「命令从未执行」⇒ 下游读成
    # 「无副作用、可直接重试」，而本函数的触发点在**执行之后**（normalize 尾）
    # ⇒ 会诱发**副作用双发**（审计 P0-3 第 1 条）。归不到证据时，保守方向是
    # 「结果未知、别盲目重试」，不是「放心重试」。
    #
    # **严格性由 commit gate 保留**：`test_fact_positions_coverage.py` 对签名表
    # 本身的失配仍以断言封死；`classify_blocked_fact()` 作为**纯函数入口**依旧
    # fail loud（生产路径走本函数的 fail-soft 兜底）。
    if not declared and not r.success:
        bit_fact = fact_from_bits(r)
        if r.blocked:
            # blocked 只接受**平台侧成因**的两格（`tools/result.py::_BLOCKED_FACT_KINDS`）：
            # 位是调用方声明的，可能是调用方成因（`command_failed`）—— 那种位在
            # blocked 语义下**不可用**（标它会向 agent 发「不是你的 bug」信号并
            # 原地重撞，L6/L19），故丢弃该位、继续向下走阶梯。
            kind: FactKind | None = (
                bit_fact
                if bit_fact in ("runner_failed", "outcome_unknown")
                else None
            )
            if kind is None:
                # 第二层：文本签名表（位缺失才参考 —— 它随措辞/语言整体失效）
                kind = classify_error_text(r.error or "")
            if kind is None and (r.extra or {}).get("timeout_kind") == "wait":
                # 第三层：代码作用域归属（审批窗口等待 ⇒ 命令从未派发），非文案
                #
                # ⚠ 必须从 ``extra`` 读，不能读 ``r.timeout_kind`` 字段 ——
                # 构造点从来没有填过那个 dataclass 字段，值一直是走 ``extra``
                # 传的（``bash.py`` 的字段白名单把它放进返回 dict，而
                # ``ToolResult.__init__`` 只把未声明的 kwargs 收进 extra）。
                # #15 审计实测：``ToolResult.err(..., timeout_kind="wait").timeout_kind
                # is None`` ⇒ 只读字段会让这一层**在生产上永不可达**，
                # 于是审批窗口等待类 blocked 结果会悄悄落到 outcome_unknown。
                kind = "runner_failed"
            if kind is None:
                # 第四层：**不猜**。落 `outcome_unknown`（=「结果未知」），并留样本。
                unclassified = note_unclassified_sample(
                    tool=tool_name,
                    error=r.error or "",
                    status=(r.extra or {}).get("status"),
                    bits=r,
                )
                kind = "outcome_unknown"
            r.fact = kind
        elif bit_fact is not None:
            # 非 blocked 的失败（shell 家族）：位是**状态**，构造点说了就得认账。
            # 此前这里无条件走 outcome_unknown ⇒ 会把调用方显式声明的
            # `runner_failed`/`command_failed` 位**抹掉**（`finalize_fact_dict`
            # 按 fact 重算派生键）⇒ 下游读不到那位（#15 的位丢失面）。
            r.fact = bit_fact
            log.info(
                "fact_position_resolved_from_bits",
                tool=tool_name,
                fact=bit_fact,
                bits=sorted(state_bits(r)),
            )
    elif declared and not r.success:
        # **只观测、不夺声明权**（#15 审计 7(a)）：构造点声明了 fact 时，
        # 位核对/文本核验/样本**全被跳过**（见上面的 `if ... and not declared`）。
        # 这是 #15 要治的「自我声明当证据」在同一层重新开口 —— 我们不改变归因
        # （构造点比通用判据更懂上下文，硬覆盖会更糟），但**声明与位冲突必须留痕**，
        # 否则「声明即免检」永远没人知道它判错了。
        bit_fact_declared = fact_from_bits(r)
        if bit_fact_declared is not None and r.fact != bit_fact_declared:
            log.warning(
                "fact_position_declared_conflicts_with_bits",
                tool=tool_name,
                declared=r.fact,
                bits_fact=bit_fact_declared,
                bits=sorted(state_bits(r)),
                action=(
                    "构造点声明的 fact 与它自己给的布尔位不一致 —— 归因**按声明**"
                    "（未改动），但请核对构造点是不是写错了。"
                ),
            )
            # F2 形态③②：冲突**必产样本**。只留 WARNING 不够 —— 它不落库，
            # 「真没冲突」与「通道哑了」永远不可分；状态判据要求
            # agent_events 里 kind='conflict' 的行数**从 0 变非 0**。
            unclassified = note_declared_sample(
                tool=tool_name,
                declared=str(r.fact),
                kind="conflict",
                bits_fact=bit_fact_declared,
                error=r.error or "",
                status=(r.extra or {}).get("status"),
                bits=r,
            )
        elif _declared_sample_due(tool_name, str(r.fact), r.error or ""):
            # F2 形态③①：非冲突的声明失败按 1/N **确定性**降采样 ——
            # 非零样本本身就是「声明支路观测通道是活的」的状态证据
            #（58-61 四轮 108 条归因样本里 shell 家族 0 条的盲区由此打开）。
            unclassified = note_declared_sample(
                tool=tool_name,
                declared=str(r.fact),
                kind="declared_sampled",
                bits_fact=bit_fact_declared,
                error=r.error or "",
                status=(r.extra or {}).get("status"),
                bits=r,
            )

    out = r.to_dict()
    if unclassified:
        # 调用方拿得到 agent_id ⇒ 由它落 agent_events（本函数是同步的、不猜 id）
        out["unclassified_sample"] = unclassified
    # ⚠ **对所有工具都跑，不是只对 shell 家族**（E21 审计 M1，2026-09-16）。
    # 旧写法是 `if tool_name in SHELL_SECURITY_LEVEL_TOOLS or judge_blocked:`，
    # 而 `judge_blocked` **恒为 True**（全仓无调用方传 False）⇒ 条件恒真。
    # 删掉那个参数时若把它"顺手收窄成 shell 家族"，会付两笔代价：
    #   ① 打红 `tests/test_p0_3_orphan_root_cause.py` —— 非 shell 工具拿不到
    #      兜底 `outcome_unknown` ⇒ `out` 连 `fact` 键都没有；
    #   ② 掐断 `fact_position` 族样本的**唯一来源**：58/59 实测 104 条
    #      未分类样本 **100% 来自非 shell 工具**（list_files/read_file/
    #      git_worktree_sync/submit_task/commit_turn/…）⇒ E23 新加的分母
    #      会结构性对应一个恒 0 的分子。
    # ⇒ 结论：这里**没有**开关，也不该有。
    # ⚠ 同上方事实位归因段（:442-459）已确立的原则（**fail loud 但不 fail hard**）：
    # `assert_fact_complete` 过去在这里**硬抛 AssertionError**，而本函数在
    # executor 的第 4 步、**dispatch 的 try/except 之外**被调用 ⇒ 未捕获异常
    # 会砸穿整条工具调用。实测（TEST_DSH_55 P0-3）：55 步以
    # `[Tool Error] AssertionError: shell tool 'X' failed without a fact
    # position` 形式炸出，并打断 agents/streaming.py 的 record_step_end ⇒
    # 115 步滞留 status='running' 后被 sweep 误判 outcome_unknown。
    # 现改为与 blocked 分支同构：记 ERROR 日志（CI/审计可捞）+ 兜底
    # `outcome_unknown`（语义=「结果未知：已记录但完成结果未持久化」）。
    # ⚠ **不得兜底 `runner_failed`**（=「命令从未执行」⇒ 下游读成「无副作用、
    # 可直接重试」）：本函数的触发点在**执行之后**（normalize 尾），命令可能
    # 已执行，标它会诱发**副作用双发**（审计 P0-3 第 1 条）。
    # **严格性仍由 commit gate 保留**：`test_fact_positions_coverage.py`
    # 对签名表本身的失配依旧以断言封死。
    try:
        assert_fact_complete(tool_name, out)
    except AssertionError as exc:
        log.error(
            "fact_position_missing_at_finalize",
            tool=tool_name,
            error_preview=(str(out.get("error") or ""))[:200],
            fallback="outcome_unknown",
            action=(
                "构造点未声明 fact —— 见 fixplan 批次 2 §1.4a；"
                "不得让断言逃逸到运行时"
            ),
            detail=str(exc)[:300],
        )
        # 兜底格是 `outcome_unknown`（=「结果未知」），**不是** runner_failed：
        # 后者语义为「命令从未执行」⇒ 下游读成「无副作用、可直接重试」。而
        # 本函数的触发点在**执行之后**（normalize 尾）⇒ 命令可能已执行，标
        # runner_failed 等于给「可能已有副作用」的步骤发安全重试通行证
        # （审计 P0-3 第 1 条；本仓库纪律：事实位错标 ⇒ 副作用双发）。
        out["fact"] = "outcome_unknown"
        # #15 审计：这条兜底（非 blocked 且构造点漏声明 fact）过去**只记日志、
        # 不产样本** ⇒ fail-loud 有两条通道、其中一条是哑的。统一到同一条：
        # 判不出来就产样本，由调用方（`_f10_result_hooks`）落 agent_events。
        if "unclassified_sample" not in out:
            out["unclassified_sample"] = note_unclassified_sample(
                tool=tool_name,
                error=str(out.get("error") or ""),
                status=None,
                # ⚠ 传 `r`（构造点声明的原始位），**禁止改成 `bits=out`** ——
                # 错法随取样点而变，两种都要防：
                # ① **就地**用 `out`：`to_dict()` 已把派生键 pop 掉
                #    （实测 `bits=out` → `['blocked', 'dialect_failed']`）
                #    ⇒ 恰好**丢掉**最该留的 `runner_failed`（丢证据）；
                # ② 把取样**挪到 `finalize_fact_dict()` 之后**再传 `out`
                #    （这是看似的顺理成章改动）：那时派生键按 `fact`
                #    **重新派生**，`runner_failed` 的 False 是**由归因结果倒推**
                #    出来的、并非构造点的声明 ⇒ 样本会显示「构造点显式声明
                #    runner_failed=False」，即**归因结果伪装成构造点的声明**
                #    —— 把诊断引向反面，比不记还糟。
                # ⇒ 无论取样点挪到哪，都只传 `r`（位在 `extra` 里，`to_dict` 不清它）。
                # 与 blocked 分支的 `bits=r` 对称（此前这里传 None ⇒ 样本里
                # `bits_present={}`，把「显式声明 False」与「啥也没说」混为一谈，
                # 正是 `bits_present` 带值要解决的那个歧义）。
                bits=r,
            )
    # 裸字典路径可能带进陈旧的 runner_failed/command_failed —— 由 fact 统一
    return finalize_fact_dict(out)


# ── 启动断言（机械 gate，import 期执行）─────────────────────────
#
# 对齐 DSH `packages/AGENTS.md:145`：
#   「Wire mechanically checkable invariants into an executed top-level gate
#     and prove each changed acceptance path rejects an invalid case」
#
# 既有先例：`llm/streamer/constants.py:232-263` 的同款 assert 风格。
# 这里的断言在**导入期**跑 —— 任何 `import hiveweave.tools.fact_positions`
# 都会执行（`tools/__init__` 与两条执行器都会导入），所以坏结构不可能
# 静默上线。
def _assert_signature_table_wellformed() -> None:
    # 1) 两张表的每一条都非空且已归一（否则 `in` 匹配会意外命中空串）
    for _fact, _sigs in _SIGNATURE_ORDER:
        assert _fact in ("runner_failed", "bad_args"), _fact
        assert _sigs, f"FactKind {_fact!r} 的签名表为空 —— 它永远不会被命中"
        for _sig in _sigs:
            assert _sig.kind in ("substr", "regex"), _sig
            assert _sig.value.strip(), f"FactKind {_fact!r} 含空签名"
            assert _sig.value == _sig.value.strip(), (
                f"签名 {_sig.value!r} 有首尾空白 —— 匹配前会归一，声明侧也必须归一"
            )
            if _sig.kind == "substr":
                assert _sig.value.lower() == _sig.value, (
                    f"签名 {_sig.value!r} 未小写 —— 子串匹配是大小写不敏感的，"
                    f"声明侧必须统一（regex 型不受此限）"
                )
            else:
                re.compile(_sig.value)  # 语法错必须在 import 期就炸（fail loud）
    # 2) 顺序铁律：runner 签名必须排在 bad_args 之前（DSH「先 runner 再 denial」）
    _order = [f for f, _ in _SIGNATURE_ORDER]
    assert _order.index("runner_failed") < _order.index("bad_args"), (
        "判据顺序错：bad_args 的宽签名会吞掉 runner 故障（见 _SIGNATURE_ORDER 注释）"
    )
    # 3) `classify_error_text` 对空/无签名输入必须返回 None（不猜）
    assert classify_error_text("") is None
    assert classify_error_text("完全无关的一段文本") is None
    # 4) 词边界判据必须**真的**带词边界（否则又退回裸子串："port" in "import"）
    for _false_hit in (
        "importerror: cannot import name 'x'",
        "operation not supported",
        "report generation failed",
    ):
        assert classify_error_text(_false_hit) is None, (
            f"{_false_hit!r} 被误命中 —— bad_args 的 port 判据退回子串形态了"
            f"（它会把 import/support/report 全归成「调用方参数错」）"
        )
    assert classify_error_text("Port 4000 is reserved for HiveWeave") == "bad_args", (
        "保留端口文案必须仍判 bad_args —— 词边界不能把真命中一起挡掉"
    )


_assert_signature_table_wellformed()


