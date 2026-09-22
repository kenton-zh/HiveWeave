"""TEST_DSH_66 Q1 回归：分支名解析放宽 ⇒ merge 义务能被结算 ⇒ approved 不挂死。

病灶（2026-09-22 实测）：`misc_tools.py` 三处就地正则
``^hw/([^/]+)/t-([0-9a-fA-F]{8})$`` 只认 ``hw/<sid>/t-<8hex>`` 一种形态，
而 `compute_branch_name` 在**无 task_id** 时产出 ``hw/<sid>/work``
（本仓主形态）⇒ 正则恒不匹配 ⇒ 三处「按分支名反查任务 ⇒ fulfill merge
义务」全部命中 0 ⇒ `verify.py:610-619`「pending merge 义务 ⇒ 不 close」
把 approved 挂死（现场：1 条义务 pending 3h46min、看门狗每 17 分钟空转）。

现场证据（照抄取证指令）：
- `git branch --list` 该项目 9 条分支 = 8 条 ``hw/A149..A156/work`` + ``main``；
  窄正则命中 **0**、``^hw/[^/]+/work$`` 命中 **8**。
- `run_steps` 里 `git_worktree_merge` 的真实回执：
  ``outcome=merged: Branch hw/A149/work merged into main``
  —— 即 47 次 merge 全部走的是「取不到 id」那条路。

本文件覆盖：
1. `parse_hw_branch` 是 `compute_branch_name` 的**逆函数**（两种合法形态都解析回）。
2. 非本形态（`main` / 空 / 缺尾段 / 非 8 位）**不得**被误判成带 id。
3. `_supersede_merge_pending_after_merge`：`hw/<sid>/work` 形态下按**分支名本身**
   做指纹（Q1 前的 else 分支语义必须保住 —— 回归不得收窄覆盖面）。
4. 阳性对照：把 `parse_hw_branch` 打回窄语义，第 1 条断言必须转红。
"""

from __future__ import annotations

import pytest

from hiveweave.services.git_worktree.naming import (
    compute_branch_name,
    parse_hw_branch,
)


# ── 1. 逆函数一致性（必须成对演进）────────────────────────────


@pytest.mark.parametrize(
    "short_id,task_id",
    [
        ("A149", "12aaa56c-1a92-415f-afec-4950d2717746"),  # → t-<8>
        ("A149", None),                                     # → work（主形态）
        ("A009", "aabbccdd-0000-0000-0000-000000000000"),
        ("A100", None),
    ],
)
def test_parse_is_inverse_of_compute(short_id, task_id):
    """`compute_branch_name` 产出的**每一条**合法分支名都必须解析得回来。

    这是 Q1 的根因断言：旧的就地正则只对 `t-<8>` 成立 ⇒ 这个参数化里
    第 2/4 组（`work`）会直接失配 —— 而它们正是本仓的实际形态。
    """
    branch = compute_branch_name(short_id, task_id)
    sid, tid = parse_hw_branch(branch)
    assert sid == short_id, f"{branch} 应解析出 short_id={short_id}"
    if task_id:
        assert tid == task_id[:8].lower(), f"{branch} 应解析出 task id 前 8 位"
    else:
        # `work` 形态**设计上不编码** task id ⇒ 必须给 None，
        # 由调用方改用 params.task_id / 按 owner 兜底。
        assert tid is None, f"{branch} 不含 task id，不得编造"


def test_work_branch_parses_short_id_but_no_task_id():
    """现场形态逐字复现：`hw/A149/work`（本项目真实分支名）。"""
    assert parse_hw_branch("hw/A149/work") == ("A149", None)


def test_t_branch_lowercases_task_id():
    """大写 id 统一小写化（否则后续 IN/LIKE 匹配假阴性）。"""
    assert parse_hw_branch("hw/A149/t-12AAA56C") == ("A149", "12aaa56c")


# ── 2. 非本形态不得被误判 ────────────────────────────────────


@pytest.mark.parametrize(
    "branch",
    [
        None,
        "",
        "main",
        "hw/A149",             # 缺尾段
        "hw//work",            # 空 sid（`[^/]+` 要求至少 1 字符 ⇒ 整体拒绝）
        "hw/A149/",            # 空尾段
        "feature/A149/work",   # 无 hw/ 前缀
    ],
)
def test_branches_outside_hw_shape_give_empty_sid(branch):
    """非 `hw/<sid>/<rest>` 形态必须整体返回哨兵（sid 也空）。

    否则会把 `main` 之类的名字当成一个有短号的 agent 分支，
    污染待清理集合（`branch_tokens`）。
    """
    assert parse_hw_branch(branch) == ("", None)


@pytest.mark.parametrize(
    "branch",
    [
        "hw/A149/t-abc",       # 非 8 位十六进制
        "hw/A149/t-12aaa56",   # 7 位
        "hw/A149/t-12aaa56cd", # 9 位
        "hw/A149/t-12aaa56c-extra",  # 尾段必须恰好
    ],
)
def test_hw_shape_with_unparseable_tail_keeps_sid_drops_id(branch):
    """sid 合法但尾段不是 `t-<8hex>` ⇒ 保留 sid、**不给 id**。

    不可截断猜测：把错误 id 写进履行判据比不履行更毒
    （`_normalize_task_id` 会把不存在的 id 静默归一成别人）。
    """
    sid, tid = parse_hw_branch(branch)
    assert sid == "A149"
    assert tid is None


def test_legacy_slug_branch_has_no_task_id():
    """legacy slug（`hw/<sid>/<task-slug>`）同样不编码 id —— 不得按 slug 猜。"""
    assert parse_hw_branch("hw/A149/fix-the-thing") == ("A149", None)


# ── 3. 结算覆盖面不得收窄（Q1 前的 else 分支语义必须保住）──────


@pytest.mark.asyncio
async def test_supersede_uses_branch_name_fingerprint_for_work_branch(monkeypatch):
    """`hw/<sid>/work` 取不到 id 时，**分支名本身**仍必须是清理指纹。

    这是「修 Q1 不得顺手收窄」的反向断言：旧代码在这里走 else 分支
    （`branch_tokens.add(str(br))`）。若把 `work` 形态也当"什么都没发生"
    跳过，[MERGE PENDING] 待办就会重新堆假账（TEST_DSH_64 #8 现场）。
    """
    import hiveweave.services.inbox as inbox_mod
    from hiveweave.tools import misc_tools

    captured: list[tuple[str, list]] = []

    class _FakeInbox:
        async def supersede_watchdog_messages(self, owner, *, prefixes, contains):
            captured.append((owner, contains))

    monkeypatch.setattr(inbox_mod, "InboxService", _FakeInbox)

    # `work` 形态取不到 task id ⇒ 不该触发按 id 反查 owner 的 DB 查询
    # （若无此保证，本用例会因真 DB 不可用而失败）。
    await misc_tools._supersede_merge_pending_after_merge(
        "proj-1", "agent-caller",
        task_id=None,
        branches=["hw/A149/work"],
    )

    assert captured, "work 形态必须仍然发出 supersede（不得静默跳过）"
    all_tokens: set[str] = set()
    for _owner, contains in captured:
        if isinstance(contains, (list, tuple, set)):
            all_tokens.update(str(t) for t in contains)
        else:
            all_tokens.add(str(contains))
    assert "hw/A149/work" in all_tokens, (
        f"应把分支名本身当指纹，实际 = {all_tokens}"
    )
    # 不得编造 task id 前缀（旧窄正则恰恰会走 else 分支，这里验证不走"编造"路）
    assert "12aaa56c" not in all_tokens


# ── 4. 两条 settle 路径语义必须一致（审计 ①-1）───────────────


@pytest.mark.asyncio
async def test_noop_branch_falls_back_to_owner_when_task_id_unsettled(monkeypatch):
    """审计 ①-1：`already_up_to_date` 早退分支的兜底必须与主路径等价。

    修前该分支是 ``if params.task_id: fulfill(...) else: fulfill_by_owner(...)``
    —— 当 ``params.task_id`` **非空但结算失败**（该任务本无 merge 义务 /
    id 已过期）时 ``fulfilled == 0``，此时**什么都不做**；而主路径用的是
    ``if not fulfilled: fulfill_by_owner(...)``，语义更完整。两条路径对
    同一个 merge 事件必须给出同一个结论，否则从早退分支进来的事故会复现
    同一个僵尸义务（本仓「统一判定源落地成逐点替换清单 ⇒ 必然漏点」）。
    """
    from hiveweave.services import obligation as oblig_mod

    calls: list[tuple] = []

    class _FakeLedger:
        async def fulfill(self, project_id, task_id, kind, **kw):
            calls.append(("fulfill", task_id))
            return 0  # 该 task 名下并无 merge 义务 ⇒ 未结算

        async def fulfill_by_owner(self, project_id, agent_id, kind, **kw):
            calls.append(("by_owner", agent_id, kw.get("short_id")))
            return 1

    monkeypatch.setattr(oblig_mod, "ObligationLedger", _FakeLedger)

    from hiveweave.tools import misc_tools

    # 直接跑**两条路径共用的那个 helper**（审计 ①-1 后它已是唯一判定源）
    # —— no-op 早退分支与主路径都调它，所以本断言对两者同时成立。
    settled = await misc_tools._settle_merge_obligations_after_merge(
        "proj-1",
        "agent-caller",
        task_id="12aaa56c-1a92-415f-afec-4950d2717746",
        branches=["hw/A149/work"],
        merge_commit="deadbeef",
    )

    kinds = [c[0] for c in calls]
    assert "fulfill" in kinds, "有 task_id 时必须先按 id 试结算"
    assert "by_owner" in kinds, (
        f"按 id 结算返回 0 后**必须**按 owner 兜底（审计 ①-1）；实际调用 = {calls}"
    )
    # 审计 ②-2：兜底必须**带范围**。分支 `hw/A149/work` 解析出 sid=A149，
    # 兜底就只能是 A149 —— 否则第三方代合时会误清该 owner 的其它 merge 义务。
    by_owner_call = next(c for c in calls if c[0] == "by_owner")
    assert by_owner_call[2] == "A149", (
        f"owner 兜底必须带 short_id 范围（审计 ②-2），实际 {by_owner_call[2]!r}；"
        f"不带范围会把该 owner 账上**其它任务**的 merge 义务一并清掉"
    )
    assert settled >= 1


@pytest.mark.asyncio
async def test_owner_fallback_is_scoped_to_the_merged_branch(monkeypatch):
    """审计 ②-2：owner 兜底**只能**清本次 merge 那条分支的义务。

    场景：架构师代合 A149 的 worktree（`taskId` 是可选参数，可缺）。
    修前 `fulfill_by_owner` 按 (owner, type) **全清** ⇒ A149 账上另一条
    无关任务的 pending merge 义务被**误判完成**，僵尸义务从此消失、
    再也没人催办。

    本用例钉住 helper 侧的契约：把分支解析出的 sid 传下去。
    """
    from hiveweave.services import obligation as oblig_mod

    seen: list[dict] = []

    class _FakeLedger:
        async def fulfill(self, project_id, task_id, kind, **kw):
            return 0

        async def fulfill_by_owner(self, project_id, agent_id, kind, **kw):
            seen.append({"agent": agent_id, "short_id": kw.get("short_id")})
            return 1

    monkeypatch.setattr(oblig_mod, "ObligationLedger", _FakeLedger)

    from hiveweave.tools import misc_tools

    await misc_tools._settle_merge_obligations_after_merge(
        "proj-1",
        "agent-architect",
        task_id=None,                      # 代合，没带 taskId
        branches=["hw/A149/work"],         # 只有分支名
        merge_commit="cafe",
    )

    assert seen, "task_id 缺失 ⇒ 必须走 owner 兜底"
    assert seen[0]["short_id"] == "A149", (
        f"兜底必须限定到本次合并的分支 sid=A149，实际 {seen[0]!r}"
    )


@pytest.mark.asyncio
async def test_owner_fallback_omits_scope_when_branch_unparseable(monkeypatch):
    """分支名解析不出 sid 时**退化**为不限定范围（诚实退化，不假装精确）。

    例如调用方只按 legacy slug 合并、或压根没传 branchName。
    此时没有更精确的判据可用 —— 退回旧语义比**猜一个** sid 安全
    （猜错 = 清错账，且是静默的）。
    """
    from hiveweave.services import obligation as oblig_mod

    seen: list[dict] = []

    class _FakeLedger:
        async def fulfill(self, project_id, task_id, kind, **kw):
            return 0

        async def fulfill_by_owner(self, project_id, agent_id, kind, **kw):
            seen.append({"short_id": kw.get("short_id")})
            return 1

    monkeypatch.setattr(oblig_mod, "ObligationLedger", _FakeLedger)

    from hiveweave.tools import misc_tools

    await misc_tools._settle_merge_obligations_after_merge(
        "proj-1", "agent-x", task_id=None, branches=["not-a-hw-branch"],
        merge_commit=None,
    )

    assert seen and seen[0]["short_id"] is None, (
        f"解析不出 sid 时不应凭空限定范围（会比旧行为更糟），实际 {seen}"
    )


def test_positive_control_narrow_regex_would_fail(monkeypatch):
    """阳性对照：把解析打回窄语义（只认 `t-<8>`），第 1 条断言必须转红。

    做法不是改生产代码，而是**证明旧判据本身不成立** —— 用与旧正则
    **逐字相同**的表达式跑同一组输入，可见 `work` 形态命中 0：
    这样"修前必红"就有可核验证据，而不是靠叙述。
    """
    import re

    old_re = re.compile(r"^hw/([^/]+)/t-([0-9a-fA-F]{8})$")
    real_branches = [f"hw/A{n}/work" for n in range(149, 157)]  # 现场 8 条

    old_hits = [b for b in real_branches if old_re.match(b)]
    new_hits = [b for b in real_branches if parse_hw_branch(b)[0]]

    assert old_hits == [], "旧正则对现场分支必须命中 0（这是 Q1 的病）"
    assert len(new_hits) == 8, "新解析器必须认全 8 条现场分支"

    # 且对 `t-<8>` 形态，新旧必须**等价**（放宽不得改变已有语义）
    t_form = "hw/A149/t-12aaa56c"
    assert bool(old_re.match(t_form)) is True
    assert parse_hw_branch(t_form) == ("A149", "12aaa56c")


# ── 5. fulfill_by_owner 的 short_id 范围（审计 ②-2）───────────


@pytest.mark.asyncio
async def test_fulfill_by_owner_short_id_scope_keeps_others_pending(tmp_path):
    """审计 ②-2 的**机制级**断言：带 short_id 时只清该 sid 的义务。

    这是本批最实质的一处收紧。修前 `fulfill_by_owner(project, owner, type)`
    按 (owner, type) **全清**；`git_worktree_merge` 的 `taskId` 可选 ⇒
    架构师代合别人 worktree 时会把对方账上**其它任务**的 pending merge
    义务一并标完成 —— 僵尸义务静默消失，再没人催办（比"多清一条"更隐蔽：
    它是**该报的错不报了**）。

    本用例直接建真表（obligations 是项目库表），造两条同 owner 不同
    short_id 的义务，验证带范围只清一条、不带范围清两条。
    """
    from unittest.mock import patch

    from hiveweave.db import project as project_db
    from hiveweave.services.obligation import ObligationLedger

    project_id = "bbbbbbbb-0000-0000-0000-000000000002"
    workspace = str(tmp_path.resolve())
    owner = "cccccccc-0000-0000-0000-000000000003"

    async def fake_ws(pid: str):
        return workspace if pid == project_id else None

    with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
        await project_db.ensure_project_db(workspace)

        now = 1_790_000_000_000
        for oid, sid, tid in (
            ("o-A149", "A149", "task-a"),
            ("o-A150", "A150", "task-b"),
        ):
            await project_db.execute_by_project(
                project_id,
                "INSERT INTO obligations (id, project_id, owner_agent_id, "
                "obligation_type, task_id, context_json, status, created_at, "
                "deadline) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    oid, project_id, owner, "merge", tid,
                    '{"short_id": "%s", "reason": "approved_needs_merge"}' % sid,
                    "pending", now, now + 3600_000,
                ],
            )

        ledger = ObligationLedger()

        # ① 带范围：只清 A149
        n = await ledger.fulfill_by_owner(
            project_id, owner, "merge", short_id="A149"
        )
        assert n == 1, f"带 short_id=A149 应只清 1 条，实际 {n}"

        # execute_by_project 的返回形态因实现而异 ⇒ 直接用只读查询取值
        conn = await project_db.get_project_db_by_project_id(project_id)
        cur = await conn.execute(
            "SELECT id, status FROM obligations WHERE obligation_type='merge' "
            "ORDER BY id"
        )
        state = {r[0]: r[1] for r in await cur.fetchall()}
        await cur.close()

        assert state["o-A149"] == "fulfilled", "本分支义务应被清"
        assert state["o-A150"] == "pending", (
            "⚠ A150 的义务**必须还在** —— 它跟本次 merge 无关。"
            "被清掉就是审计 ②-2 的误清（僵尸义务静默消失）"
        )

        # ② 不带范围：退回旧语义，清掉剩下的那条
        n2 = await ledger.fulfill_by_owner(project_id, owner, "merge")
        assert n2 == 1, f"不带范围应清剩余 1 条，实际 {n2}"

        cur = await conn.execute(
            "SELECT COUNT(*) FROM obligations WHERE obligation_type='merge' "
            "AND status='pending'"
        )
        remaining = (await cur.fetchone())[0]
        await cur.close()
        assert remaining == 0, "不带范围时旧语义（全清）必须保持"

        async with project_db._ensure_lock:
            c = project_db._cache.pop(workspace, None)
        if c is not None:
            try:
                await c.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_fulfill_by_owner_unparseable_context_is_not_cleared(tmp_path):
    """context_json 坏掉/缺 short_id 的行，在带范围时**不清**（保守）。

    理由：宁可留一条该清的义务（看门狗会催，可修），也不要误清一条
    不该清的（静默消失，不可修）。这是"失败方向要选可观测的那边"。
    """
    from unittest.mock import patch

    from hiveweave.db import project as project_db
    from hiveweave.services.obligation import ObligationLedger

    project_id = "bbbbbbbb-0000-0000-0000-000000000004"
    workspace = str(tmp_path.resolve())
    owner = "cccccccc-0000-0000-0000-000000000005"

    async def fake_ws(pid: str):
        return workspace if pid == project_id else None

    with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
        await project_db.ensure_project_db(workspace)
        now = 1_790_000_000_000
        await project_db.execute_by_project(
            project_id,
            "INSERT INTO obligations (id, project_id, owner_agent_id, "
            "obligation_type, task_id, context_json, status, created_at, "
            "deadline) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                "o-broken", project_id, owner, "merge", "task-c",
                "{not valid json", "pending", now, now + 3600_000,
            ],
        )

        ledger = ObligationLedger()
        n = await ledger.fulfill_by_owner(
            project_id, owner, "merge", short_id="A149"
        )
        assert n == 0, f"context_json 坏掉的行不得被带范围的兜底清掉，实际清 {n}"

        conn = await project_db.get_project_db_by_project_id(project_id)
        cur = await conn.execute(
            "SELECT status FROM obligations WHERE id='o-broken'"
        )
        st = (await cur.fetchone())[0]
        await cur.close()
        assert st == "pending", "坏 context 行必须保持 pending（可观测，可修）"

        async with project_db._ensure_lock:
            c = project_db._cache.pop(workspace, None)
        if c is not None:
            try:
                await c.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_ambiguous_two_sids_does_not_fall_back_to_owner(monkeypatch):
    """审计 B4：分支指向**两个不同 sid** 时不兜底（宁可漏清，不可误清）。

    触发形态：`branches=[branch, branch_name]`（`misc_tools.py` 两处调用点
    都是这么传的）里两个值解析出**不同的** sid —— 说明调用方传错了分支，
    此时"兜底全清"正是审计 ②-2 要治的误清。

    本用例钉住：**不调用 `fulfill_by_owner`**（既不限定范围、也不全清）。
    代价是该清的义务没清（留 pending，看门狗会催，可观测）；
    收益是绝不静默清掉别人账上无关的义务。
    """
    from hiveweave.services import obligation as oblig_mod

    calls: list[str] = []

    class _FakeLedger:
        async def fulfill(self, project_id, task_id, kind, **kw):
            calls.append(f"fulfill:{task_id}")
            return 0

        async def fulfill_by_owner(self, project_id, agent_id, kind, **kw):
            calls.append(f"by_owner:{kw.get('short_id')}")
            return 1

    monkeypatch.setattr(oblig_mod, "ObligationLedger", _FakeLedger)

    from hiveweave.tools import misc_tools

    settled = await misc_tools._settle_merge_obligations_after_merge(
        "proj-1",
        "agent-architect",
        task_id=None,
        branches=["hw/A149/work", "hw/A150/work"],   # 两个不同 sid ⇒ 语义不明确
        merge_commit=None,
    )

    assert settled == 0, f"语义不明确时不得结算出条数，实际 {settled}"
    assert not any(c.startswith("by_owner") for c in calls), (
        f"⚠ 语义不明确时**不得**调 owner 兜底（会误清），实际调用 = {calls}"
    )


@pytest.mark.asyncio
async def test_single_sid_with_bare_short_id_param_still_scopes(monkeypatch):
    """现场主形态回归：`["hw/A149/work", "A149"]` ⇒ 仍须限定到 A149。

    现场实测（47 条 merge）：`branchName` 常传**裸 short_id**（`'A149'` 6 次），
    此时 `branch_name` 就是 `"A149"`（非 `hw/` 形态，解析出 sid 为空），
    而 `branch` 是 `"hw/A149/work"`（sid=A149）。
    ⇒ 去重后 **只有 1 个 sid**，必须正常限定 —— 不能被"多 sid 就拒"误伤。
    """
    from hiveweave.services import obligation as oblig_mod

    seen: list = []

    class _FakeLedger:
        async def fulfill(self, project_id, task_id, kind, **kw):
            return 0

        async def fulfill_by_owner(self, project_id, agent_id, kind, **kw):
            seen.append(kw.get("short_id"))
            return 1

    monkeypatch.setattr(oblig_mod, "ObligationLedger", _FakeLedger)

    from hiveweave.tools import misc_tools

    await misc_tools._settle_merge_obligations_after_merge(
        "proj-1", "agent-x", task_id=None,
        branches=["hw/A149/work", "A149"],
        merge_commit=None,
    )

    assert seen == ["A149"], (
        f"`branch` 与裸 short_id 指向同一 sid ⇒ 应限定 A149，实际 {seen}"
    )
