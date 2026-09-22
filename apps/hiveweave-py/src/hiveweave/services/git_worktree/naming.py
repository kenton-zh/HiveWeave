"""Branch naming helpers for git worktrees."""
from __future__ import annotations

import re

from .constants import (
    SLUG_MAX_LEN,
    _SLUG_INVALID,
    _SLUG_SPACE,
    _SLUG_TRIM,
)

# ── 解析侧：`compute_branch_name` 的**逆函数**（必须成对演进）─────────────
#
# 为什么不能收窄成 `t-([0-9a-fA-F]{8})`（TEST_DSH_66 Q1，2026-09-22 实测）：
# `compute_branch_name` 有**两条**合法形态 —— 有 task_id 时
# `hw/<sid>/t-<tid8>`，**无 task_id 时 `hw/<sid>/work`**（每个 agent 一条
# 稳定工作分支）。收窄正则只认前者 ⇒ 后者**恒不匹配**。
#
# 现场证据：TEST_DSH_66 的 9 条分支 = 8 条 `hw/A149..A156/work` + `main`，
# 而 `run_steps` 里 `git_worktree_merge` 的真实回执是
# `outcome=merged: Branch hw/A149/work merged into main` —— 即 **47 次
# merge 全部走的是这条"取不到 id"的路**。后果不是文案不符，而是
# `misc_tools` 里三处「按分支名反查任务 ⇒ fulfill merge 义务」**全部命中 0**
# ⇒ 义务永不 fulfill ⇒ `verify.py` 的「pending merge 义务 ⇒ 不 close」
# 把 approved 挂死（该项目 1 条义务 pending 3h46min、8 条却 fulfilled 正常）。
_HW_BRANCH_RE = re.compile(r"^hw/(?P<sid>[^/]+)/(?P<rest>.+)$")
# 从 `rest` 里取 task id 前 8 位（仅 `t-<8hex>` 形态有；`work`/slug 形态没有）
_HW_TASK_ID_RE = re.compile(r"^t-([0-9a-fA-F]{8})$")


def parse_hw_branch(branch: str | None) -> tuple[str, str | None]:
    """把 `hw/<sid>/<rest>` 拆成 ``(short_id, task_id8_or_None)``。

    只认 `hw/` 前缀形态；非本形态返回 ``("", None)``。

    ``task_id8`` 仅当尾段恰为 ``t-<8位十六进制>``（小写化）时给出 ——
    `work` 与 legacy slug（`hw/<sid>/<task-slug>`）**设计上就不编码 task id**
    （见 `compute_branch_name` / `_branch_name`），此时必须返回 None，
    由调用方改用其它判据（caller 传入的 task_id / 按 owner 兜底），
    **不能**把 `work` 当成 id 或按 slug 猜。
    """
    if not branch:
        return "", None
    m = _HW_BRANCH_RE.match(str(branch).strip())
    if not m:
        return "", None
    rest = m.group("rest")
    tid = _HW_TASK_ID_RE.match(rest)
    return m.group("sid"), (tid.group(1).lower() if tid else None)

def _slugify(name: str) -> str:
    """Slugify a task name (契约 09 slugify 规则).

    1. 空格/正反斜杠 → "-"
    2. 删除非 [a-zA-Z0-9_-] 和 CJK 以外字符
    3. 截断至 40 字符
    4. 去除首尾连字符
    5. 空串 → "task"
    """
    s = _SLUG_SPACE.sub("-", name)
    s = _SLUG_INVALID.sub("", s)
    s = s[:SLUG_MAX_LEN]
    s = _SLUG_TRIM.sub("", s)
    return s or "task"


def _branch_name(short_id: str, task_name: str) -> str:
    """LEGACY slug 命名 (P0 之前) — 仅为兼容存量分支保留。

    新代码一律用 compute_branch_name(); 本函数只在解析/清理
    老 slug 分支 (hw/<sid>/<task-slug>) 时作兜底。
    """
    return f"hw/{short_id}/{_slugify(task_name)}"


def compute_branch_name(short_id: str, task_id: str | None = None) -> str:
    """稳定分支命名 (P0) — 从 task_id 派生, 与任务描述文本无关。

    - 有 task_id → ``hw/<shortId>/t-<task_id 前 8 位小写>``
      (同一任务重算必同名, 根治 description[:40] 每次重算导致的分支增生)
    - 无 task_id → ``hw/<shortId>/work`` (每个 agent 一条稳定工作分支)
    """
    tid = (task_id or "").strip().lower()
    if tid:
        return f"hw/{short_id}/t-{tid[:8]}"
    return f"hw/{short_id}/work"
