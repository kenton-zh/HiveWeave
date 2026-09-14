"""VERIFY 种类的**一次性回填**迁移（#11 阶段 B 的前半）。

## 它做什么

`tasks.kind` 在阶段 A 只是加了列，存量行全是 NULL。本模块按**旧判据**
（任务标题前缀）把**存量** VERIFY 行回填成 `kind="verify"`。

为什么必须回填：阶段 B 之后运行时判定改读 `kind`，**不回填就会让存量 VERIFY
全部变成"普通任务" ⇒ 隔离门 / MAIN 证据闸 / 串行锁静默敞开**，而且不报错。

## 为什么回填要**照抄旧判据、连它的误判一起抄**

验收③要求「老项目迁移后行为与迁移前**逐条一致**」。旧判据会把「标题恰好像
VERIFY 的普通任务」也当成 VERIFY —— 那是**当时的实际行为**。若回填时"顺手修正"
（只回填"真的是 VERIFY 的"），就等于**在迁移里偷偷改了行为**，而这类改动没有
任何显式信号。⇒ 照抄。真正的修正应该发生在**创建侧**（`kind` 由平台写），
不是在这里。

## 一次性语义靠**时间锚**，不靠标记表 —— 以及为什么

本仓既有的迁移标记是 `(workspace, 连接世代)` 键（见
`db.project.schema_marker_key_for_project`）⇒ 每个新世代都会重跑。而回填
**不能重跑**：cutover 之后新建的普通任务若标题恰好像 VERIFY，重跑会把它
**静默升格**成 VERIFY —— 这正是 #11 要防的伪造的镜像。

时间锚是**纯函数**：只处理 `created_at < CUTOVER_MS` 的行 ⇒ 重跑是 no-op，
且永远不碰新行。**不新增标记表/列、不新增机制。**

## ⚠ 边界（写清楚，别当成万能）

- **时钟偏移**：`created_at` 是写入机的本地时间。若某台机器的钟走快，存量
  VERIFY 的 `created_at` 可能 ≥ cutover ⇒ 被漏掉（不会回填，也不会报错）。
  ⇒ 故本模块对「cutover 之后、`kind` 为空、标题仍像 VERIFY」的行**记 warning**
  （它要么是正常任务恰好叫这个名字 = 预期，要么是时钟偏移漏掉的存量 VERIFY = 要查）。
- 本模块只被 `services/tasks/db.py::_ensure_schema` 调用。
"""

from __future__ import annotations

import re

import structlog

from .verify import VERIFY_KIND

log = structlog.get_logger(__name__)

#: **旧**判据（任务标题前缀）—— 从 `services/tasks/verify.py` 搬来，逐字保留。
#: 阶段 B 的翻转提交会把它从 `verify.py` 删除，届时这里成为它**唯一**的存身处
#: （「迁移必须有终点」：回填完成后运行时不再调用任何标题判据）。
_LEGACY_VERIFY_TITLE_RE = re.compile(r"^[【\[]?\s*VERIFY\s*[:：]")

#: 一次性语义的时间锚：只处理**此前创建**的行。
#: 取值 = 本迁移上线时刻（2026-09-14 20:40 +08:00）。**改动它等于让回填重跑**，
#: 只有在"确实需要重新覆盖某个时间窗"时才动，并且要重新论证上面那条不能重跑的理由。
CUTOVER_MS = 1789399200000

#: 单条 UPDATE 里最多塞多少个 id（避免 SQL 变量数爆掉 / 语句过长）。
_CHUNK = 200


def is_legacy_verify_title(title: str | None) -> bool:
    """**旧**判据：标题是否像 VERIFY（仅回填使用，运行时禁止调用）。"""
    return isinstance(title, str) and bool(_LEGACY_VERIFY_TITLE_RE.match(title))


async def backfill_verify_kind(project_id: str) -> dict[str, int]:
    """回填存量 VERIFY 行的 `kind`；返回统计（供日志/测试断言）。

    幂等：重复调用是 no-op（命中行已被 `kind IS NULL` 排除）。
    """
    from .db import _execute, _query

    rows = await _query(
        project_id,
        "SELECT id, title FROM tasks WHERE kind IS NULL AND created_at < ?",
        [CUTOVER_MS],
    )
    # ⚠ `_query` 返回 ``sqlite3.Row``（无 ``.get``）⇒ 用下标取，键都在 SELECT 里。
    matched = [r["id"] for r in rows if is_legacy_verify_title(r["title"])]
    for i in range(0, len(matched), _CHUNK):
        chunk = matched[i : i + _CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        await _execute(
            project_id,
            f"UPDATE tasks SET kind = ? WHERE kind IS NULL AND id IN ({placeholders})",
            [VERIFY_KIND, *chunk],
        )

    # fail-loud：cutover 之后仍以旧形态出现、且 kind 为空的行。见模块 docstring
    # 「边界」—— 这是时钟偏移的唯一可观测信号，不能静默。
    newer = await _query(
        project_id,
        "SELECT id, title FROM tasks WHERE kind IS NULL AND created_at >= ?",
        [CUTOVER_MS],
    )
    post_cutover = [r["id"] for r in newer if is_legacy_verify_title(r["title"])]
    if post_cutover:
        log.warning(
            "verify_kind_backfill_post_cutover_matches",
            count=len(post_cutover),
            task_ids=post_cutover[:5],
            action=(
                "cutover 之后仍有 kind 为空、标题像 VERIFY 的任务。正常情形是"
                "「普通任务恰好叫这个名字」（无动作）；若这里有**存量** VERIFY，"
                "说明写入机时钟偏移使 created_at ≥ cutover ⇒ 需人工回填。"
            ),
        )

    return {
        "scanned": len(rows),
        "backfilled": len(matched),
        "post_cutover_matches": len(post_cutover),
    }
