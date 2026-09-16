"""``.hiveweave/{shared,reports,drafts,handoffs}`` 的跨树策略**单一权威**。

判据来源（**我们自己的设计**，不是外部参照）：
- ``services/git_worktree/service_create.py:99-105`` —— 四目录**反选入库**
  （``.gitignore`` 的 ``!.hiveweave/shared/`` 等），跨 worktree 可见可合并；
- ``service_create.py:239-245``（生成的 ``.gitattributes``）—— 各子目录的
  **合并策略**：``shared/**/*.md`` = ``merge=binary``（双方改动即冲突）、
  ``drafts/handoffs`` = ``union``、``reports/`` = 默认**文本**合并；
- ``services/acl_sandbox/policy.py:54`` —— executor 的授权树根 = worktree
  ⇒ **隔离是有的**，读侧只跨越它，不拆掉它。

⇒ **候选序由各子目录的合并策略推出，不能全员照抄 reports 的顺序**：

======================  =============  ==================  ====================
子目录                  合并策略        权威落点             读侧候选序
======================  =============  ==================  ====================
``reports/``            文本            MAIN（多方写）       MAIN → 本树 → 兄弟树
``shared/``             binary         **无**（改动即冲突）  **本树 → MAIN → 兄弟树**
``drafts/``/``handoffs`` union          MAIN（累积更全）     未开跨树读（见下）
======================  =============  ==================  ====================

``shared`` 取"本树优先"的理由：binary 合并下**没有单一权威落点**，若 MAIN 优先，
"本树已有新版、MAIN 还是旧版"时会**读到旧版**（把自己的新改动读丢）。

**范围与已知近似（说清边界，别当"已全覆盖"）**：
1. 跨树读本批只开 ``shared`` / ``reports`` 两个子目录（``cross_tree_read``）。
   ``drafts``/``handoffs`` **刻意不开**，判据来自 prompts 与既有 docstring（不是推测）：
   ``prompts/executor.py:545`` 明写 ``.hiveweave/reports/`` 与 ``.hiveweave/drafts/``
   是 **individual**（个人产物）；``handoffs/`` 的既有语义是「**上级** read_file 读
   下级的交接文档」（``tools/file.py::_check_hiveweave_dir`` docstring，审计
   2026-08-05 P0）。两者都**没有**"本树读不到就去别的树找"的契约，跨树回退会把
   "我还没写的草稿 / 不属于我的交接"变成别人的内容 —— 伪影比缺失更贵。
   ⚠ **同批发现（既有漂移，未在本批改）**：``prompts/executor.py:545`` 把
   ``reports/`` 也说成 individual，与 reports 的"MAIN 权威落点"读侧语义相反；
   已记入 ``fixqueue`` 观察项，不在本批范围内。
2. 顺序按**子目录**定，而 ``.gitattributes`` 的真实规则是**按扩展名**
   （``shared/**/*.md`` 才是 binary；``shared/`` 下的非 .md 走默认文本合并）。
   本模块按子目录近似 —— ``shared`` 里的契约文档就是 ``.md``，非 .md 共享
   文件沿用同一顺序（"读自己的最新版"在两种策略下都不算坏）。**这是近似，
   不是等价**；若日后出现大量非 .md 共享产物，应按扩展名细分。
   ⚠ 近似带来的**不是**新缺口：本树存在时"本树命中优先"与**改动前**行为一致
   （改动前 shared 根本不跨树），差异只出现在"本树缺失"这一侧。
"""
from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass

HIVEWEAVE_DIR = ".hiveweave"
# 派生自 ``service_create.py:160-172`` 的 ``.gitignore`` 反选清单。
SHARED_SUBDIRS: tuple[str, ...] = ("shared", "reports", "drafts", "handoffs")


@dataclass(frozen=True)
class SubdirPolicy:
    """一个共享子目录的读侧策略（顺序 + 是否跨树 + 文案主语）。"""

    merge: str          # ``.gitattributes`` 里的合并策略（事实描述，供诊断/测试）
    local_first: bool   # 读侧候选序：本树是否优先（仅 cross_tree_read=True 时被消费）
    cross_tree_read: bool
    artifact: str       # miss 文案里"这不是不存在的证明"的主语


POLICIES: dict[str, SubdirPolicy] = {
    "reports": SubdirPolicy("text", False, True, "evidence"),
    "shared": SubdirPolicy("binary", True, True, "file"),
    "drafts": SubdirPolicy("union", False, False, "draft"),
    "handoffs": SubdirPolicy("union", False, False, "handoff"),
}


def policy_for(subdir: str) -> SubdirPolicy | None:
    return POLICIES.get(subdir)


def local_first_for(subdir: str) -> bool:
    """读侧是否"本树优先"（仅 ``cross_tree_read=True`` 的子目录有意义）。"""
    pol = POLICIES.get(subdir)
    return bool(pol and pol.local_first)


def cross_tree_read_enabled(subdir: str) -> bool:
    pol = POLICIES.get(subdir)
    return bool(pol and pol.cross_tree_read)


def normalize_shared_rel(path: str) -> str:
    """共享路径的**唯一接受形态**：逐段归一（剥 ``./``、归约 ``..``）。

    ⚠ 为什么"归一"是安全前提而不是洁癖：``rel`` 会被 ``os.path.join`` 原样保留、
    再由 ``realpath`` **在另一棵树上**重新解释 ⇒ ``.hiveweave/shared/../data.db``
    在 MAIN 上落成 ``.hiveweave/data.db``（平台被保护的库），而本树侧那一次守卫
    看到的是 ``<worktree>/.hiveweave/data.db``（落在 ``worktrees/`` 白名单内 ⇒
    放行）——**守卫检查的路径与实际读的路径不是同一条**。
    09-16 审计双路独立实测：``read_file(".hiveweave/shared/../data.db")`` 一度
    读到 MAIN 的 DB；``.hiveweave/shared/../../../../victim.txt`` 读到项目外。
    归一之后这两条都**不再是共享路径**（``shared_subdir_of`` 返回 None）⇒ 退回
    普通解析，受 ``_inside_any`` / ``_check_hiveweave_dir`` 常规守卫。
    同族纪律见 ``services/vision.py::resolve_screenshot_path_multi_tree``
    （它本就拒绝含 ``..`` 段的相对路径）。
    """
    s = (path or "").replace("\\", "/")
    if not s:
        return ""
    norm = posixpath.normpath(s)
    return "" if norm == "." else norm


def shared_subdir_of_parts(parts: tuple[str, ...] | list[str]) -> str | None:
    """路径段序列里 ``.hiveweave`` 紧随的共享子目录名（None = 不是共享路径）。

    **按任意位置匹配**（不只开头）：worktree 里解析出来的共享路径形状是
    ``<root>/.hiveweave/worktrees/<sid>/.hiveweave/shared/...`` —— 前一个
    ``.hiveweave`` 是项目级、后一个才是该树自己的。旧实现（``file.py`` 的
    ``shared_hint``）就是靠"任意位置"匹配才能同时覆盖 MAIN 与叶子两种布局。

    ⚠ 判据只认**归一后的段对**：调用方必须传 ``normalize_shared_rel()`` 的
    结果（``shared_subdir_of`` 已经这么做了）。传给本函数**未归一**的段序列
    等于把 ``..`` 放进来 —— 那正是本批审计抓到的高危路径。
    """
    for i in range(len(parts) - 1):
        if parts[i] == HIVEWEAVE_DIR and parts[i + 1] in SHARED_SUBDIRS:
            return parts[i + 1]
    return None


def shared_subdir_of(path: str) -> str | None:
    """``.hiveweave/<subdir>/...`` 形态的**相对路径** → subdir（否则 None）。

    先归一再判（见 :func:`normalize_shared_rel`）。旧实现用
    ``str.lstrip("./")`` 剥前导 —— 那是把参数当**字符集**，会把
    ``.hiveweave/...`` 剥成 ``hiveweave/...`` 从而恒不命中（本仓已有四处
    同族修正，见 ``tools/file.py::strip_dot_slash_prefix``）。

    ⚠ **两个形态硬条件**（09-16 二轮审计实测的两条越权读，都为修此事）：
    1. 归约后必须以 ``.hiveweave/`` **开头** —— 否则 ``../.hiveweave/shared/x``
       这类"从上层目录折回来"的路径会被判成共享路径，而它在候选树上拼出来的
       是**项目外**的文件（实测读到，且回执误标 ``[read from MAIN]``）。
    2. 段序列里只认**紧跟** ``.hiveweave`` 的那个子目录名 —— 见
       :func:`shared_subdir_of_parts`。
    '..' 段归约后仍在中间的（``a/../b``）由 ``posixpath.normpath`` 消掉，
    消不掉的必然落在开头，被条件 1 挡住。
    """
    rel = normalize_shared_rel(path)
    if not rel.startswith(f"{HIVEWEAVE_DIR}/"):
        return None
    return shared_subdir_of_parts(rel.split("/"))


def _sibling_worktree_roots(root: str) -> list[str]:
    """``<root>/.hiveweave/worktrees/*``（跳过 ``_`` 前缀的兜底/隔离目录）。

    没有 ``worktrees/`` = 单树布局（非必然异常）：兄弟树本就不存在，静默降级
    （旧实现打一条 debug 事件 ``vision.sibling_trees_unavailable``，全仓
    **零消费者**，随本批一并去掉）。
    ``root`` 为空 → 直接空列表：`os.path.realpath("")` 是**进程 CWD**，拿它去
    找 ``.hiveweave/worktrees`` 会把工作目录的树混进候选集。
    """
    if not root or not str(root).strip():
        return []
    base = os.path.join(os.path.realpath(root), HIVEWEAVE_DIR, "worktrees")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        # 没有 worktrees/ = 单树布局（非必然异常）：兄弟树本就不存在。
        return []
    out: list[str] = []
    for name in names:
        if name.startswith("_"):        # _quarantine（git_worktree/constants.py:9）
            continue
        full = os.path.join(base, name)
        if os.path.isdir(full):
            out.append(full)
    return out


def ordered_tree_roots(
    root: str | None,
    workspace: str | None,
    *,
    local_first: bool,
) -> list[str]:
    """跨树候选序（realpath + 去重、保序）：本树/MAIN 谁先由 ``local_first`` 定。

    ``root`` 未给时以 ``workspace`` 为项目根（单树布局：去重后只剩一棵）。
    兄弟 worktree 恒在最后 —— 它是"可能写了它的树"的**近似上界**
    （``dispatch_pin.py:7,34`` 的同一命名空间），不是权威落点。

    ⚠ **请求者树必须排在兄弟树之前**（两种顺序都是）。这是 ``fixplan:351``
    的「MAIN → 请求者 → assignee」。旧的两份实现（``tools/file.py`` 与
    ``services/vision.py``）在"请求者树不是排序第一个兄弟"时会**给出不同的
    顺序** —— 本函数是唯一权威，两处都消费它。
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(base: str | None) -> None:
        if not base or not str(base).strip():
            return
        try:
            rp = os.path.realpath(base)
        except (OSError, ValueError):
            return
        key = os.path.normcase(rp)
        if key in seen:
            return
        seen.add(key)
        out.append(rp)

    if local_first:
        _add(workspace)
        _add(root)
    else:
        _add(root)
        _add(workspace)
    for sib in _sibling_worktree_roots(root or workspace or ""):
        _add(sib)
    return out


# ── miss 文案（按子目录分派；一份文案不能同时讲 reports 与 shared）──────
#
# 两个纪律：
# 1. **不下全局结论**（#5 / L17/L20 同族病）：只报"查过哪些树"，不写
#    "确实不存在"——本函数只跑了候选集，没有依据对全世界下结论。
# 2. **话术必须与该子目录的策略一致**：对着 ``shared``（binary、无权威落点）
#    讲"权威落点在 MAIN"会把 agent 引到旧版上（09-16 前 READ_MISS_HINT
#    正是这样：一份字符串同供 read_file/list_dir，内容却只讲 reports）。
_MISS_HINTS: dict[str, str] = {
    "reports": (
        " `.hiveweave/reports/**` 是平台自管共享产物（默认文本合并 ⇒ 权威落点"
        "= MAIN），读侧按 MAIN → 本树 → 兄弟树 查找，并在回执说明在哪棵树命中。"
    ),
    "shared": (
        " `.hiveweave/shared/**` 是跨 worktree 共享区（merge=binary：双方改动即"
        "冲突 ⇒ **无单一权威落点**），读侧按 **本树 → MAIN → 兄弟树** 查找并在"
        "回执点名命中的树；空目录不入库，本树没有它是常态。"
        " The .hiveweave/shared/ chain may not be materialized in your worktree "
        "yet (empty shared/ is not tracked). Write: write_file to "
        ".hiveweave/shared/<file> → checkpoint → merge; members see it after "
        "their next worktree merge."
    ),
}


def miss_hint_for(subdir: str, tried: list[str] | None = None) -> str:
    """该子目录的 miss 教学文案；``tried`` 为本次真查过的树标签（可空）。"""
    hint = _MISS_HINTS.get(subdir, "")
    if not hint:
        return ""
    if tried:
        hint += f" 本次已查 {len(tried)} 棵树：{', '.join(tried)}。"
    return hint


# 跨树命中时的归因后缀（跟着 `[read from <树>]`）。**按子目录分派** ——
# 与 miss 文案同理：拿 reports 的"权威落点在 MAIN"讲 shared 会把 agent
# 引到旧版上（09-16 审计抓到：这段曾对 shared 硬编码 reports 的说明）。
_HIT_NOTES: dict[str, str] = {
    "reports": (
        " — shared .hiveweave/reports/ is written to MAIN by design"
        " (remote worktrees are visible to all agents; see git_worktree"
        " shared 4-dir contract)"
    ),
    "shared": (
        " — .hiveweave/shared/ is cross-worktree (merge=binary: no single"
        " authoritative copy), so this is the copy that existed in that tree,"
        " not a merge of all of them"
    ),
}


def hit_note_for(subdir: str) -> str:
    """跨树命中回执的归因后缀（未知子目录 → 通用一句）。"""
    return _HIT_NOTES.get(
        subdir,
        " — this file lives in another tree of the same project",
    )
