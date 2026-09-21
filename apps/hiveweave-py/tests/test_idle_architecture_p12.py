"""P1/P2 idle architecture: Wait Contract, merge window, spawn proxy, metrics."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hiveweave.db import project as project_db
from hiveweave.services import task as task_module
from hiveweave.services.process_registry import (
    clear_registry_for_tests,
    prepare_spawn_command,
)
from hiveweave.services.telemetry import telemetry
from hiveweave.services.turn_result import WaitingOnItem
from hiveweave.services.wait_contract import (
    WaitContractService,
    category_to_wake_event,
    event_matches_waits,
    obligation_version,
)
from hiveweave.services.wake_policy import should_wake


PROJECT_ID = "test-p12-waits"
AGENT_ID = "agent-wait-1"


@pytest.fixture
async def wait_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_path = str(Path(tmpdir).resolve())
        # Init minimal project DB
        await project_db.ensure_project_db(workspace_path)

        async def fake_ws(pid: str):
            return workspace_path if pid == PROJECT_ID else None

        task_module._migrated.clear()
        from hiveweave.services import wait_contract as wc

        wc._migrated.clear()

        with patch("hiveweave.db.meta.get_project_workspace", fake_ws):
            yield {"project_id": PROJECT_ID, "workspace": workspace_path}

        async with project_db._ensure_lock:
            conn = project_db._cache.pop(workspace_path, None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_wait_contract_persist_and_match(wait_env):
    svc = WaitContractService()
    pid = wait_env["project_id"]
    created = await svc.replace_waits(
        pid,
        AGENT_ID,
        [WaitingOnItem(kind="user", ref="user", note="验收")],
        phase="waiting",
        obligations=[{"id": "t1", "status": "running"}],
    )
    assert len(created) == 1
    assert "user_message" in created[0]["wakeOn"]
    assert created[0]["obligationVersion"] == obligation_version(
        [{"id": "t1", "status": "running"}]
    )

    active = await svc.list_active(pid, AGENT_ID)
    assert len(active) == 1

    assert await event_matches_waits(active, event="user_message") is True
    assert await event_matches_waits(active, event="ask_reply") is False

    # Wake policy with contracts
    assert (
        should_wake(
            "command",
            disposition="waiting_human",
            from_agent_id="user",
            active_waits=active,
        )
        is True
    )
    assert (
        should_wake(
            "ask",
            disposition="waiting_human",
            from_agent_id="peer-1",
            active_waits=active,
        )
        is True
    )

    n = await svc.clear_waits(pid, AGENT_ID)
    assert n >= 1
    assert await svc.list_active(pid, AGENT_ID) == []


@pytest.mark.asyncio
async def test_wait_ref_matches_flower_name_not_just_uuid():
    """Regression: commit_turn stores 花名; inbox from_agent_id is UUID."""
    waits = [
        {
            "kind": "agent",
            "ref": "天线",
            "wakeOn": ["ask_reply", "message_from_ref", "timeout"],
        }
    ]
    # UUID alone must NOT match 花名
    assert (
        await event_matches_waits(
            waits,
            event="message_from_ref",
            from_agent_id="c9dd2fad-299c-409a-9a93-236324f7d9d5",
        )
        is False
    )
    # With name → wake
    assert (
        await event_matches_waits(
            waits,
            event="message_from_ref",
            from_agent_id="c9dd2fad-299c-409a-9a93-236324f7d9d5",
            from_agent_name="天线",
            from_short_id="A002",
        )
        is True
    )
    assert (
        should_wake(
            "command",
            disposition="waiting_agent",
            from_agent_id="c9dd2fad-299c-409a-9a93-236324f7d9d5",
            from_agent_name="天线",
            from_short_id="A002",
            active_waits=waits,
        )
        is True
    )
    # Wrong agent still wakes (product: any message wakes)
    assert (
        should_wake(
            "command",
            disposition="waiting_agent",
            from_agent_id="other-uuid",
            from_agent_name="潮汐",
            from_short_id="A003",
            active_waits=waits,
        )
        is True
    )


def test_command_pierces_timer_external_waits():
    """CEO npm-install nudge must wake blocked 白鹭 despite timer wait."""
    waits = [
        {
            "kind": "timer",
            "ref": "alarm-1",
            "wakeOn": ["alarm", "timeout"],  # legacy contract without message_from_ref
        }
    ]
    assert (
        should_wake(
            "command",
            disposition="blocked",
            from_agent_id="ceo-uuid",
            from_agent_name="归零",
            from_short_id="A001",
            active_waits=waits,
        )
        is True
    )
    assert (
        should_wake(
            "progress",
            disposition="blocked",
            from_agent_id="ceo-uuid",
            from_agent_name="归零",
            active_waits=waits,
        )
        is True
    )

def test_category_to_wake_event():
    assert category_to_wake_event("command", from_agent_id="user") == "user_message"
    assert category_to_wake_event("task_transition") == "task_transition"
    assert category_to_wake_event("ask", from_agent_id="a1") == "ask_reply"


def test_spawn_proxy_rejects_bare_vite_without_port():
    """裸 vite 无端口 → 拒斥 + 给 `start_dev_server` 处方（2026-09-21 P0-1）。

    **本测试取代原 `test_spawn_proxy_rewrites_bare_vite`**（原断言「平台代其
    改写、尾部追加 --port」）。行为为何改向「拒斥」：

      平台对**同一语义**原本并存两种**相反**策略 ——
      `check_command_reserved_ports`（工具侧）拒斥 + 给处方，
      `prepare_spawn_command`（旧 :833）代其改写。
      取「拒斥 + 处方」这一支：它是平台自己写好的出口，且导向显式通道
      `start_dev_server`（工具已存在、内部零文本判据）。

    旧改写策略的两条害处（TEST_DSH_65 实证）：
      ① 判据 `\\bvite\\b` 无段首锚 ⇒ 误命中 `.vite` 目录名 / `vite.config.ts`
         文件名，把正常命令改成非法命令（跨项目 120 条 / 21 项目）；
      ② 它抢在拒斥之前命中 ⇒ agent 永远收不到处方，学不会走显式通道。
    """
    clear_registry_for_tests()
    cmd, env, err, _inj = prepare_spawn_command(
        "npx vite --host 0.0.0.0", project_id="p-spawn"
    )
    assert err is not None
    assert "start_dev_server" in err          # 处方必须给出出口
    assert "5173" in err                      # 说明为何拒（默认回落到保留端口）
    assert cmd == "npx vite --host 0.0.0.0"   # **不得**代其改写
    assert "--port" not in cmd


def test_spawn_proxy_leaves_dev_server_with_port_alone():
    """阳性对照：带端口的真启动**不得**被新拒斥误伤。

    没有这条，守卫可能退化成「一律拒」而照样绿（假绿）。
    """
    clear_registry_for_tests()
    cmd, env, err, _inj = prepare_spawn_command(
        "npx vite --port 3001 --strictPort", project_id="p-spawn"
    )
    assert err is None
    assert cmd == "npx vite --port 3001 --strictPort"


def test_bare_dev_server_judge_does_not_match_paths_or_filenames():
    """P0-1 回归守卫：`.vite` 目录名 / `vite.config.ts` 文件名不得被判成启动命令。

    原判据 `\\bvite\\b` 无段首锚 —— 词边界在 `.` 与 `v` 之间成立，因此
    `Get-ChildItem .vite -Name`、`cat vite.config.ts` 全被命中，平台据此把
    整条命令尾部追 `--port <P> --strictPort`（TEST_DSH_65 本项目 11 次、
    跨项目 120 条 / 21 项目）。本测试钉住该回归：判据必须带段首锚。
    """
    from hiveweave.services.process_registry import _BARE_DEV_SERVER_RE

    for text in (
        "Get-ChildItem .vite -Name",
        "cat vite.config.ts",
        "grep -r define vite.config.ts",
        "ls .vite/deps",
        "echo vite",
        "vitest run",
    ):
        assert not _BARE_DEV_SERVER_RE.search(text), text

    # 阳性对照：真启动仍必须被认出，否则上面是「一律不命中」的假绿
    for text in ("npx vite", "npx vite --host 0.0.0.0", "npm run dev", "cd app && npx vite"):
        assert _BARE_DEV_SERVER_RE.search(text), text


def test_dev_server_judge_ignores_finite_output_subcommands():
    """有限输出子命令不得被判成 dev server 而**拒死**（审计 2026-09-21 P1）。

    `npx vite build` / `npm run dev:test` / `npx vite --version` 跑完即退，
    不是长驻服务。同族的 flask / uvicorn / app.server 三个分支**都带**
    `_SPAWN_BLOCKING_VERB_RE` + `_UVICORN_HELP_RE` 两道排除，原 vite 分支
    两条都漏 —— 加了段首锚后行为从「改坏命令」变成「直接拒死」，本条钉住它。
    """
    from hiveweave.services.process_registry import check_command_reserved_ports

    for text in (
        "npx vite build",
        "npm run dev:test",
        "npx vite --version",
        "npx vite --help",
    ):
        assert check_command_reserved_ports(text) is None, text

    # 阳性对照：真正的长驻裸 dev server 仍必须被拒，否则上面是「一律放行」的假绿
    for text in ("npx vite", "npm run dev", "pnpm dev"):
        assert check_command_reserved_ports(text) is not None, text
    # ⚠ `vite preview` 是**长驻**静态服务器（默认 4173 亦在 RESERVED_PORTS 内）
    # ⇒ 必须仍被拒，不得混进「跑完即退」那批一起放行。
    assert check_command_reserved_ports("npx vite preview") is not None


def test_prepare_spawn_rejects_routed_dev_server_without_port():
    """兜底：上游已按 dev server 路由、但命令最终无端口 ⇒ 拒（不得裸起 5173）。

    上游路由判据（`bash.py` 的 `_DEV_SERVER_TRIGGER_RE`）锚含 `\\s`，比本模块的
    `_BARE_DEV_SERVER_RE`（严格段首锚）**宽** ⇒ `npx --yes vite` / `sudo npx vite`
    / `timeout 10 npx vite` 会被路由却不被判据命中。没有本兜底，它们会实绑
    5173（= 平台保留端口）。**该兜底不依赖任何文本判据** —— 它只看「路由方
    期望有端口，而最终没有端口」这个事实。
    """
    clear_registry_for_tests()
    for cmd in ("npx --yes vite", "sudo npx vite", "timeout 10 npx vite"):
        got_cmd, _env, err, _inj = prepare_spawn_command(
            cmd, project_id="p-routed", routed_as_dev_server=True
        )
        assert err is not None, cmd
        assert "start_dev_server" in err, cmd   # 处方必须给出出口
        assert got_cmd == cmd, cmd              # 不得代其改写

    # 阳性对照：带端口的同类命令必须放行，否则兜底是「一律拒」的假绿
    _c, _e, err_ok, _i = prepare_spawn_command(
        "npx --yes vite --port 3100 --strictPort",
        project_id="p-routed",
        routed_as_dev_server=True,
    )
    assert err_ok is None

    # 阳性对照：未声明为 dev server 的普通命令不得被这条兜底波及
    _c2, _e2, err_plain, _i2 = prepare_spawn_command(
        "echo hello", project_id="p-routed", routed_as_dev_server=False
    )
    assert err_plain is None

    # ⚠ 真实路径覆盖（审计 2026-09-21 P2-a）：必须用 routed=True 断言 `--version`
    # / `--help` **不**被拒。`bash.py` 对它们不路由，但真实调用链里 routed 是
    # 由 `_detect_dev_server_command` 决定的，只在 `check_command_reserved_ports`
    # 上断言会**假绿** —— 早期实现正是在这条路径上把它们拒掉。
    for cmd in ("npx vite --version", "npx vite --help"):
        _ch, _eh, err_help, _ih = prepare_spawn_command(
            cmd, project_id="p-routed", routed_as_dev_server=True
        )
        assert err_help is None, cmd


def test_dev_server_judge_cannot_be_bypassed_by_unrelated_words():
    """判据内化：别处出现阻塞动词或注释**不得**解除拒斥（审计 2026-09-21 P1-b）。

    原实现用 `_SPAWN_BLOCKING_VERB_RE` 对**整条命令** search 来实现「有限输出
    子命令」的排除 ⇒ `npm install && npm run dev` 里别处的 `install` 让排除
    生效 ⇒ 裸起 5173；在注释里写一句 `# TODO test` 同样能解除拒斥。
    这等于用一个新的文本旁路换掉旧的 —— 排除必须**贴着它要约束的那一段**
    （现由 `vite` 段后的负向前瞻 + `dev(?:\\s|$)` 尾部约束实现）。
    """
    from hiveweave.services.process_registry import check_command_reserved_ports

    for text in (
        "npm install && npm run dev",
        "npm ci && npm run dev",
        "npx vite --host 0.0.0.0 # TODO test",
        "npx vite --host 0.0.0.0 # then lint",
        "npm run install && pnpm dev",
    ):
        assert check_command_reserved_ports(text) is not None, text


def test_prepare_spawn_injects_port_for_routed_server_families():
    """注入族在 `routed_as_dev_server=True` 下必须**照常注入端口**。

    ⚠ 本条钉的是**兜底块的位置**（第三轮审计 P1）：兜底一度被放在四个注入分支
    **之前**，于是 `uvicorn` / `flask` / `app.server` 这些**由下面分支负责注入
    端口**的命令，在 `extra_env["PORT"]` 尚未设置时就被判「无端口」⇒ 工具路径
    （恒传 `routed=True`）全部拒死。注入族此前在 routed=True 下**零覆盖**
    （`test_dev_server_uvicorn.py` 从不传该参数），所以那个错位**没有测试能照出**。
    """
    clear_registry_for_tests()
    for cmd, needle in (
        ("uvicorn app.main:app", "--port"),
        ("uvicorn app.main:app --reload", "--port"),
        ("flask run", "--port"),
    ):
        got_cmd, env, err, _inj = prepare_spawn_command(
            cmd, project_id="p-inject", routed_as_dev_server=True
        )
        assert err is None, (cmd, err)
        assert needle in got_cmd, (cmd, got_cmd)
        assert env.get("PORT"), cmd

    # `python -m app.server` 只注入 PORT env（不改命令 —— 它可能不认 uvicorn 参数）
    got_cmd, env, err, _inj = prepare_spawn_command(
        "python -m app.server", project_id="p-inject", routed_as_dev_server=True
    )
    assert err is None
    assert env.get("PORT")


def test_spawn_proxy_rejects_reserved_explicit():
    cmd, env, err, _inj = prepare_spawn_command(
        "npx vite --port 5173", project_id="p-spawn"
    )
    assert err is not None
    assert "5173" in err


def test_telemetry_counters():
    telemetry.reset_counters_for_tests()
    telemetry.agent_wake("a1", "user")
    telemetry.agent_wake("a1", "trigger")
    telemetry.agent_no_progress("a1", streak=2)
    telemetry.inbox_deduped("a1", "progress")
    snap = telemetry.snapshot_counters()
    assert snap["wake_total"] == 2
    assert snap["wake_by_reason"]["user"] == 1
    assert snap["wake_by_reason"]["trigger"] == 1
    assert snap["no_progress_faults"] == 1
    assert snap["inbox_deduped"] == 1
    telemetry.reset_counters_for_tests()


@pytest.mark.asyncio
async def test_merge_window_coalesces_triggers():
    from hiveweave.agents.agent import Agent, AgentState

    agent = object.__new__(Agent)
    agent.id = "merge-1"
    agent.status = AgentState.IDLE
    agent._message_queue = []
    agent._MERGE_WINDOW_MS = 0
    agent._lock = __import__("asyncio").Lock()

    chats: list[tuple] = []

    async def fake_chat(msg, opts=None):
        chats.append((msg, opts or {}))
        return {"ok": True}

    agent.chat = fake_chat  # type: ignore

    await agent.enqueue_wake(
        "wake1",
        {"trigger": True, "inbox_msg_ids": ["m1"], "source": "t"},
    )
    await agent.enqueue_wake(
        "wake2",
        {"trigger": True, "inbox_msg_ids": ["m2", "m1"], "source": "t"},
    )
    await agent.enqueue_wake("user hi", {"trigger": False})

    await Agent._drain_message_queue(agent)

    assert len(chats) == 1
    msg, opts = chats[0]
    assert msg == "wake2"
    assert opts.get("merged_wakes") == 2
    assert opts.get("inbox_msg_ids") == ["m2", "m1"]  # last wake only (ACK=seen)
    # user still queued
    assert len(agent._message_queue) == 1
    assert agent._message_queue[0][0] == "user hi"


def test_matched_agent_wait_wakes_even_if_waiting_human():
    """Peer reply that satisfies an agent-wait must pierce waiting_human.

    Regression: CEO waited on 潮汐/墨染 while disposition=waiting_human;
    their module-split replies matched Wait Contract but wake_policy still
    returned False → inbox wake=0 → org deadlock.
    """
    waits = [
        {
            "kind": "agent",
            "ref": "潮汐",
            "wake_on": ["ask_reply", "message_from_ref", "timeout"],
            "phase": "waiting",
        }
    ]
    cat = "command"
    assert (
        should_wake(
            cat,
            disposition="waiting_human",
            from_agent_id="uuid-chaoxi",
            from_agent_name="潮汐",
            from_short_id="A003",
            active_waits=waits,
        )
        is True
    )
    # Unrelated peer still wakes (product: any message wakes)
    assert (
        should_wake(
            cat,
            disposition="waiting_human",
            from_agent_id="uuid-other",
            from_agent_name="路人",
            from_short_id="A099",
            active_waits=waits,
        )
        is True
    )
