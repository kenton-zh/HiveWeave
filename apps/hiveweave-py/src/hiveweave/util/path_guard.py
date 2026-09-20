"""path_guard.py —— 越出**授权树**的统一判定（shell 与 file 共用）。

## 这个模块管什么（以及**不管**什么）

管：命令/路径的**效果落点是否越出本 agent 的授权树**。

**判据出处（我们自己的模型，不是外部参照）**：
- ``services/acl_sandbox/policy.py:54`` —— ``boundary_root``
  「授权树根（**executor=worktree** / 项目根角色=项目根，realpath）」。
  ⇒ 「``.hiveweave/worktrees/<**非本树** id>/``」不是一个新发明的规则，
  它**正是 boundary_root 已经在判的事**（fixplan §10.3）。
- ``services/acl_sandbox/policy.py:57`` —— agent 私有 ``temp_dir``；
- ``services/git_worktree/service_create.py:99-105`` —— 跨树共享四目录
  ``.hiveweave/{shared,reports,drafts,handoffs}`` 反选入库；
- ``services/git_worktree/dispatch_pin.py:7,34`` —— worktree 落点
  ``.hiveweave/worktrees/<assignee>``；
- ``services/git_worktree/constants.py:9`` —— ``QUARANTINE_DIR`` 隔离兜底。

## ⚠️ 明确不做的事（已定的反面）

**不做 DSH 式的"单棵树内统一 sandbox"**。DSH 的 ``FsSandboxController``
管的是**单棵树内**的路径，**没有"是不是本 agent 的树"这个概念** —— 照它
改会做出一个**在多 worktree 下错误**的统一 sandbox（fixplan §0.5 / §10.3
明确废弃该引用）。我们共用的是**"越出授权树"的判定**，不是"单树 sandbox"。

⇒ 因此本模块**不需要**"统一拦截一切路径"的能力，只需要**一个可复用的
越界判定 + 一份统一处方**，让 shell 与 file 两个入口给出**同一句话**。
"""

from __future__ import annotations

import os
from pathlib import Path

# ── 幽灵嵌套 worktree（自嵌套树）─────────────────────────────────────
# 症状：agent 的 workspace 本身就是 worktree
# （``<project>/.hiveweave/worktrees/<id>``）时，一个**重复了 worktree 前缀**
# 的相对路径（如 ``.hiveweave/worktrees/<id>/src/x.py``）会解析成
# ``<project>/.hiveweave/worktrees/<id>/.hiveweave/worktrees/<id>/src/x.py``。
# 写侧会 ``mkdir(parents=True)`` **静默**造出一棵幽灵树
# （M4 — slack-clone_03 A044 start_dev_server 失败）。
#
# 判定以 **workspace 自身**为基准：相对路径中出现 ``.hiveweave`` 段**仅当
# 后随 ``worktrees`` 段**且（workspace 自身已在 worktree 内，或项目根
# workspace 已见过首个合法 ``worktrees`` 段）才算幽灵：
#   - 同 id 重复：``…/worktrees/<id>/.hiveweave/worktrees/<id>/…``
#   - 跨 id：``…/worktrees/A044/.hiveweave/worktrees/A045/…``
#     （不同 id 各一次，"同 id 计数"法漏判，实测可无限交替加深幽灵树）
# ``.hiveweave/shared|reports|tool_outputs|…`` 段**不判幽灵**：worktree 内的
# agent 必须能读写平台自管目录（executor 把大工具输出落盘
# ``<ws>/.hiveweave/tool_outputs/`` 并回传句柄 —— 见 .hiveweave 就拒会切断
# 该契约）。
def double_worktree_prefix(workspace_path: str, full_path: str) -> str | None:
    """检测 ``full_path`` 里的**自嵌套 worktree 前缀（幽灵树）**。

    返回触发幽灵的段名（``.hiveweave`` / ``.Hiveweave``）或 None。
    签名与语义与 ``tools/file.py::_double_worktree_prefix`` **完全一致** ——
    本函数即其抽取，file 侧保留同名薄封装以免调用点漂移。
    """
    try:
        ws = Path(workspace_path).resolve()
        full = Path(full_path).resolve()
    except (OSError, ValueError):
        return None
    try:
        rel_parts = full.relative_to(ws).parts
    except ValueError:
        return None
    # workspace 自身是否已在 worktree 内（<project>/.hiveweave/worktrees/<id>）
    ws_cf = [p.casefold() for p in ws.parts]
    ws_in_worktree = any(
        ws_cf[i] == ".hiveweave" and ws_cf[i + 1] == "worktrees"
        for i in range(len(ws_cf) - 1)
    )
    cf = [p.casefold() for p in rel_parts]
    seen_wt = False  # 已见过 .hiveweave/worktrees 段（项目根 workspace 首个合法）
    i = 0
    while i < len(cf):
        if cf[i] != ".hiveweave":
            i += 1
            continue
        if i + 1 < len(cf) and cf[i + 1] == "worktrees":
            # .hiveweave/worktrees/<id> 段：
            # - workspace 自身已在 worktree 内 → 相对路径再现 worktrees
            #   前缀 = 幽灵嵌套（同 id / 跨 id / 多层交替全拦）
            # - 项目根 workspace → 首个合法（单次出现），再现 = 幽灵
            if ws_in_worktree or seen_wt:
                return rel_parts[i]
            seen_wt = True
            i += 2
            continue
        # .hiveweave/shared|tool_outputs|reports|… 段：合法（平台自管目录，
        # worktree 内 agent 也必须能读 tool_outputs 落盘句柄）——不判幽灵
        i += 1
        continue
    return None


#: 越出授权树的统一中文处方（shell 与 file **共用同一句话**）。
#: 平台纪律：**不猜译、不改写**用户命令 —— 只拒发 + 指路。
OUT_OF_BOUNDARY_HINT = (
    "该路径指向**别的 worktree**（``.hiveweave/worktrees/<非本树 id>/…``），"
    "已越出你的授权树根（executor 的授权树 = 你自己的 worktree，见 "
    "acl_sandbox/policy.py 的 boundary_root）。"
    "平台自管共享产物（``.hiveweave/reports/``、``.hiveweave/shared/`` 等"
    "四目录）会由读侧自动跨树查找并说明在哪棵树命中，**不需要**手写别的树"
    "的路径；写自己的文件请用相对路径（相对你所在的那棵树）。"
    # TEST_DSH_64 #10①：补官方时序指路——此前只讲读侧与相对路径，没讲
    # 「想在合并前验证还没 merge 的实现怎么办」，agent 只能试错撞墙。
    "合并前要验证叶子实现：等 merge 后在 MAIN 验证，或先 "
    "``git_worktree_merge(dryRun=true)`` 查前置条件；跨树只读需求请该树 "
    "assignee 代跑/贴结果。"
)


def worktree_id_in_path(path: str) -> str | None:
    """从路径里取出 ``.hiveweave/worktrees/<id>`` 的 ``<id>``（无则 None）。

    用于判断「路径指向了**哪一棵** worktree」——多树归因的最小事实。
    """
    s = (path or "").replace("\\", "/")
    parts = [p for p in s.split("/") if p]
    for i in range(len(parts) - 2):
        if parts[i].casefold() == ".hiveweave" and parts[i + 1].casefold() == "worktrees":
            return parts[i + 2]
    return None


def is_foreign_worktree_ref(path: str, workspace_path: str) -> bool:
    """``path`` 是否显式指向**另一棵树**的 worktree 目录。

    判据（``acl_sandbox/policy.py:54``）：授权树根 = 本 agent 的 worktree。
    ⇒ 路径里出现 ``.hiveweave/worktrees/<id>`` 且 ``<id>`` **不是**
    workspace 自身所在的那棵树 = 效果落点越出授权树。

    非 worktree 路径（普通项目文件）返回 False —— 普通路径的写隔离由
    ``_resolve_safe`` / ACL 沙箱各自负责，本函数只管「跨树引用」这一维。

    平台自管兜底目录（``QUARANTINE_DIR`` = ``.hiveweave/worktrees/_quarantine``，
    ``git_worktree/constants.py:9``）**不是别的 agent 的树** —— 它是平台自己
    搬迁隔离用的目录，不由任何 agent 拥有。⇒ ``_`` 前缀 id 一律放行，与
    ``tools/file.py:548`` 兄弟树扫描、``services/vision.py:204`` 的同一豁免
    对齐（否则平台自管目录会被 shell/file 两个入口当"越出授权树"拦截）。
    """
    target_id = worktree_id_in_path(path)
    if not target_id:
        return False
    if target_id.startswith("_"):
        # 平台自管（_quarantine 等），非任何 agent 的树 ⇒ 非跨树引用
        return False
    own_id = worktree_id_in_path(workspace_path)
    if own_id is None:
        # 项目根角色（CEO/HR/bash_main）：授权树根 = 项目根，
        # 指向任意 worktree 都是**读侧审查**的合法形态，不算越界。
        return False
    return own_id.casefold() != target_id.casefold()


def resolve_within(boundary_root: str, candidate: str) -> str | None:
    """把 ``candidate`` 解析为 ``boundary_root`` 内的绝对路径；越界返回 None。

    与 ``tools/file.py`` 的写侧沙箱同语义，本函数是其**共享版**，
    供 shell 侧做「效果落点」判定时复用（避免两处各写一套漂移）。
    """
    if not boundary_root:
        return None
    try:
        base = os.path.realpath(boundary_root)
    except (OSError, ValueError):
        return None
    if not candidate:
        return None
    try:
        cand = os.path.realpath(candidate)
    except (OSError, ValueError):
        return None
    if cand == base:
        return cand
    try:
        if os.path.commonpath([cand, base]) == base:
            return cand
    except ValueError:
        return None
    return None
