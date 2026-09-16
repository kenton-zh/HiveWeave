"""0-3（2026-09-16）：让 ``HIVEWEAVE_GIT_HARDENED`` 有**消费者**。

## 为什么有这份文件

`util/win_subprocess.apply_git_hardening` 会把 7 个 git 配置键注入每次
git/shell spawn 的 env，并用 `HIVEWEAVE_GIT_HARDENED` 标记自证幂等。
但改造前那个标记**除了"注入器自证幂等"以外零下游** —— 于是 git 自毁时
无法归因：实证 **#23** 两项目 22 次 `external diff died` 全发生在 agent
自己的 shell 里，而事后**无法回答**「那次 git 到底有没有跑在加固环境里」。
（横向家族：`worktree_error` / `bits_present` 同族 —— 写了事实位没有消费者。）

修法：把「这次 spawn 的 env 实际带没带加固」变成一条**事实位**
（`run_steps.git_hardened`，从 env 构造点读出来），而不是对代码路径的推断。

## 本文件的判据形态

- 用具名常量 `GIT_HARDENING_MARK` 与**真实子进程**（子进程自己 echo 标记）
  证明「预测 == 实际」，不是读代码得出的。
- 正反两侧都跑：shell 入口（判据 True）与普通可执行（判据 False）。
  反向对照让"永远为真"的假守卫无法通过：若漏斗无差别加固，第二条会红。
"""

from __future__ import annotations

import os
import sys

import pytest
import structlog.testing

from hiveweave.util.win_subprocess import (
    GIT_HARDENING_MARK,
    apply_git_hardening,
    git_hardened,
    git_hardening_applies,
    hidden_run,
)

from tests.test_idle_architecture_p0 import EXEC, task_env  # noqa: F401


@pytest.fixture
def ledger_env(task_env):
    """`task_env` + 把 agent→workspace 映射塞进 project_db 的缓存。

    `record_step_start` 走 `get_project_db_for_agent(agent_id)`：它第 1 步读
    `_agent_cache`、缓存未命中才查 Meta DB。`task_env` 只 patch 了项目侧的
    workspace 查询，没有注册 agent ⇒ 不塞这个缓存会拿到
    「agent not registered in Meta DB」。走缓存是这条查找**设计上的合法路径**，
    不是绕过判据。
    """
    from hiveweave.db import project as project_db

    ws = task_env["workspace"]
    project_db._agent_cache[EXEC] = ws
    yield task_env
    project_db._agent_cache.pop(EXEC, None)


def _clean_env() -> dict[str, str]:
    """os.environ 去掉加固标记。

    测试进程本身可能就跑在**被加固过的**父进程下（平台 spawn 过它），
    不剥掉这一步，反向对照会在子进程里读到父进程留下的标记而**假红**
    （断言 NONE 却读到 1）—— 方向是假红，不是假绿。
    """
    return {k: v for k, v in os.environ.items() if k != GIT_HARDENING_MARK}


def _child_prints_mark(argv: list[str]) -> str:
    """经漏斗起真实子进程，让它自己报告 env 里有没有标记。

    ⚠ `hidden_run` 转发给 `subprocess.run` ⇒ **argv 必须整体作一个位置参数**
    （写成 `hidden_run(*argv)` 会变成多个位置参数，报
    `TypeError: bufsize must be an integer`）。
    """
    proc = hidden_run(
        argv,
        env=_clean_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return (proc.stdout or "").strip()


# ── 1. 判据与事实必须一致（真实子进程，正反两侧）──────────────────


def test_shell_entry_is_hardened_and_child_can_see_the_mark():
    """shell 入口 ⇒ 判据 True，且**子进程自己**读得到标记 == "1"。"""
    argv = ["cmd", "/c", f"echo %{GIT_HARDENING_MARK}%"]
    assert git_hardening_applies(tuple(argv)) is True

    out = _child_prints_mark(argv)
    assert out == "1", out
    # 回执戳与"实测"必须同源（`_run_native` 的 `git_hardened` 就是这么取的）
    assert git_hardened(apply_git_hardening(_clean_env())) is True


def test_plain_executable_is_not_hardened_and_child_sees_nothing():
    """★ 反向对照：普通可执行（非 git/非 shell 入口）⇒ 判据 False，
    子进程 env 里**没有**标记。

    **没有这条，第一条可能是"永远为真"的假守卫** —— 若漏斗无差别加固，
    第一条照样绿。
    """
    argv = [
        sys.executable,
        "-c",
        f"import os;print(os.environ.get('{GIT_HARDENING_MARK}','NONE'))",
    ]
    assert git_hardening_applies(tuple(argv)) is False

    out = _child_prints_mark(argv)
    assert out == "NONE", out


def test_predicate_covers_git_and_shell_entries():
    """判据口径（不依赖机器上有没有 git.exe）。"""
    assert git_hardening_applies(("git", "diff", "a.txt")) is True
    assert git_hardening_applies(("git.exe", "--version")) is True
    assert git_hardening_applies(("pwsh", "-Command", "ls")) is True
    assert git_hardening_applies(("node", "--test")) is False
    assert git_hardening_applies(()) is False


def test_predicate_rejects_raw_string_instead_of_silently_false():
    """传裸字符串必须 fail loud。

    `tuple("cmd /c git status")` 会退化成逐字符元组、head 是 `'c'` ⇒ 静默
    `False`。公开函数的误用不该无声（本仓「静默失效是一等缺陷」）。
    """
    with pytest.raises(TypeError):
        git_hardening_applies("cmd /c git status")


def test_apply_git_hardening_is_idempotent_on_marked_env():
    """幂等：同一 env 接两层不得让槽位翻倍（加固键是追加语义）。

    这条同时是「`_run_native` 先把加固落进 env、下层漏斗再调一次」这个
    写法**子进程环境不变**的依据。
    """
    once = apply_git_hardening(_clean_env())
    twice = apply_git_hardening(dict(once))
    assert once["GIT_CONFIG_COUNT"] == twice["GIT_CONFIG_COUNT"]
    assert git_hardened(twice) is True


# ── 2. 工具层真的把事实位带出来了（穿透真实执行入口）──────────────


@pytest.mark.asyncio
async def test_run_native_reports_measured_git_hardened(tmp_path):
    """★ 穿透 `bash._run_native`（真实 spawn）：结果必须带 `git_hardened=True`。

    判据落在**结果字典的状态**上，不是"代码里写了这一行"。
    """
    from hiveweave.tools.bash import _run_native

    result = await _run_native("echo hi", str(tmp_path), 30)
    assert result.get("exit_code") == 0, result
    assert result.get("git_hardened") is True, result


@pytest.mark.asyncio
async def test_run_native_reports_false_when_hardening_is_skipped(
    tmp_path, monkeypatch
):
    """★ 证明那个值是**从 env 读出来的**，不是硬写 `True`。

    把 `apply_git_hardening` 换成不写标记的透传版 ⇒ `_run_native` 必须报
    `False`。没有这条，「实测 vs 硬编码」这两者无法区分（审计实测：
    `_run_native` 三条分支全是 shell 入口，真跑起来恒 True）。
    """
    import hiveweave.util.win_subprocess as wsub
    from hiveweave.tools.bash import _run_native

    monkeypatch.setattr(
        wsub, "apply_git_hardening", lambda env: dict(env), raising=True
    )
    result = await _run_native("echo hi", str(tmp_path), 30)
    assert result.get("exit_code") == 0, result
    assert result.get("git_hardened") is False, result


def test_shell_tool_result_success_path_keeps_observation_bits():
    """★ **成功**出口也必须带观测位（0-3 审计实测的真缺口）。

    改造前成功分支只传 `public`，把 `fact_flags` 整个丢掉 ⇒
    `git_hardened` / `injection_applied` 在**每一条成功命令**上落 NULL，
    而这两列的文档写的是「NULL = 不适用/未判定」—— 把"适用且成立"记成
    "不适用"就是 NULL 说谎。
    """
    from hiveweave.tools.bash import _shell_tool_result

    ok = _shell_tool_result(
        success=True, blocked=False, output="hi", error="",
        banner="", suffix="", public={"enforcement": "native"},
        fact_flags={"git_hardened": True, "injection_applied": True,
                    "fact": None, "runner_failed": None},
    ).to_dict()
    assert ok["git_hardened"] is True, ok
    assert ok["injection_applied"] is True, ok
    # 归因位不得出现在成功结果上（result.py 不变式 2 会抛）
    assert "runner_failed" not in ok and "fact" not in ok, ok

    # 反向对照：失败出口本来就带（同时钉住"改成功分支别把失败分支改坏"）
    err = _shell_tool_result(
        success=False, blocked=False, output="", error="boom",
        banner="", suffix="", public={"enforcement": "native"},
        fact_flags={"git_hardened": True},
    ).to_dict()
    assert err["git_hardened"] is True, err


def test_spawn_stamp_passes_git_hardened_through():
    """`_enforcement_stamp` 必须把 `git_hardened` 搬进最终结果。

    这是「登记一次」的地方（登记点在 `policy.SPAWN_STAMP_KEYS`）：
    漏登记 ⇒ 字段在这一层被静默过滤（本仓踩过两次 —— `runner_failed`
    与 `dialect_failed` 都因此恒 None）。
    """
    from hiveweave.tools.bash import _enforcement_stamp

    stamped = _enforcement_stamp(
        {"git_hardened": False, "enforcement": "confined", "noise": 1}
    )
    assert stamped == {"git_hardened": False, "enforcement": "confined"}
    # 缺键时**不补默认值**：缺 = 不适用/未判定，不是"确认未加固"
    assert _enforcement_stamp({"enforcement": "native"}) == {"enforcement": "native"}


def test_sandbox_env_carries_the_mark(tmp_path):
    """受限路径的 env 构造点（`_build_sandbox_env`）也必须带标记 ——
    `spawn_confined` 就是从这里读"实测"值的。"""
    from hiveweave.services.acl_sandbox.service import _build_sandbox_env

    env = _build_sandbox_env(
        str(tmp_path), str(tmp_path / "cache"), str(tmp_path / "temp"), None
    )
    assert git_hardened(env) is True, sorted(env)[:5]
    assert env[GIT_HARDENING_MARK] == "1"
    assert env["GIT_CONFIG_COUNT"] not in ("", "0", None)


# ── 3. 事实位真的落库（run_steps.git_hardened）──────────────────


@pytest.mark.asyncio
async def test_project_db_has_git_hardened_column(ledger_env):
    """迁移生效：列存在，且**不给存量行回填 0**（NULL = 未判定）。"""
    from hiveweave.db import project as project_db

    conn = await project_db.ensure_project_db(ledger_env["workspace"])
    rows = await (await conn.execute("PRAGMA table_info(run_steps)")).fetchall()
    cols = {r[1]: r for r in rows}   # PRAGMA: (cid, name, type, notnull, dflt, pk)
    assert "git_hardened" in cols, sorted(cols)
    # 无 DEFAULT ⇒ 存量/缺省行是 NULL（"没这条信息" ≠ "确认未加固"）
    assert cols["git_hardened"][4] is None, cols["git_hardened"]
    # 建表自检也要登记（漏一处 = 新库缺列而静默断账）
    from hiveweave.db.schema import PROJECT_DB_COLUMN_CHECKS, PROJECT_DB_TABLES

    assert "git_hardened" in PROJECT_DB_COLUMN_CHECKS["run_steps"]
    # ⚠ ALTER 也必须在迁移清单里：只改 CREATE TABLE ⇒ **新库有列、老库永远没有**
    # （本仓 TEST_DSH_37 P0-① 就是排错位的 ALTER 被静默吞掉，274 次调用零记账）。
    assert any(
        s.strip().endswith("ALTER TABLE run_steps ADD COLUMN git_hardened INTEGER")
        for s in PROJECT_DB_TABLES
    ), "迁移未登记 —— 已有项目库永远不会长出这一列"


@pytest.mark.asyncio
async def test_record_step_end_writes_git_hardened_and_none_keeps_old_value(
    ledger_env,
):
    """写入语义：True→1、False→0、None→**不覆盖**（COALESCE 保留既有值）。

    这条钉住的是「未判定不得冒充否」—— 若 None 写成 0，一条从未判定的步骤
    会被读成"确认未加固"，那比没有这列更糟。
    """
    from hiveweave.db import project as project_db
    from hiveweave.services.run_ledger import RunLedger

    ledger = RunLedger()
    conn = await project_db.ensure_project_db(ledger_env["workspace"])

    async def _mk() -> str:
        sid = await ledger.record_step_start(
            agent_id=EXEC, run_id="run-0-3", step_index=0,
            step_type="tool_call", tool_name="bash",
        )
        assert sid, "record_step_start returned no id"
        return sid

    async def _val(sid: str):
        cur = await conn.execute(
            "SELECT git_hardened FROM run_steps WHERE id = ?", [sid]
        )
        row = await cur.fetchone()
        return None if row is None else row[0]

    # ① True → 1
    sid1 = await _mk()
    await ledger.record_step_end(
        agent_id=EXEC, step_id=sid1, status="completed", git_hardened=True,
    )
    assert await _val(sid1) == 1

    # ② False → 0（是"确认未加固"，与 NULL 不同）
    sid2 = await _mk()
    await ledger.record_step_end(
        agent_id=EXEC, step_id=sid2, status="completed", git_hardened=False,
    )
    assert await _val(sid2) == 0

    # ③ None → 不覆盖既有 1
    await ledger.record_step_end(
        agent_id=EXEC, step_id=sid1, status="completed", git_hardened=None,
    )
    assert await _val(sid1) == 1, "None 覆盖了既有值 —— 未判定被写成了否"

    # ④ 非 spawn 类步骤不写 ⇒ NULL（不适用）
    sid3 = await _mk()
    await ledger.record_step_end(agent_id=EXEC, step_id=sid3, status="completed")
    assert await _val(sid3) is None, "非 spawn 步骤被写入了默认值"


# ── 4. 加固**降级**也必须被看见（同族：静默失效是一等缺陷）─────────


def test_degraded_hardening_emits_event_when_hooks_dir_unavailable(monkeypatch):
    """`_empty_hooks_dir` 拿不到数据根 ⇒ 只注入了部分键。

    改造前这条路径**完全静默**（`git_hardening_pairs` 里 `if hooks:` 直接跳过）
    ⇒ 「只注入了 7 键里的一部分」与「注入了全套」在数据上长得一样。
    """
    import hiveweave.util.win_subprocess as wsub

    monkeypatch.setattr(wsub, "_HARDENING_PAIRS", None)
    monkeypatch.setattr(wsub, "_DEGRADED_WARNED", False)
    monkeypatch.setattr(wsub, "_empty_hooks_dir", lambda: None)

    with structlog.testing.capture_logs() as logs:
        pairs = wsub._hardening_pairs_cached()
        # 失败不缓存 ⇒ 再调两次（模拟后续 spawn）：不得再打 warning
        wsub._hardening_pairs_cached()
        wsub._hardening_pairs_cached()

    assert not any(k == "core.hooksPath" for k, _ in pairs)
    hits = [e for e in logs if e.get("event") == "git_hardening_degraded"]
    # ⚠ `capture_logs` **不按级别过滤**（debug 也会被收进来）⇒ 判据必须落在
    # 级别上，而不是"总条数"：重复调用只允许剩 debug。
    warned = [e for e in hits if e.get("log_level") != "debug"]
    assert len(warned) == 1, (
        "降级告警必须只打一次（失败不缓存 ⇒ 否则故障期每个 spawn 一条，"
        f"把日志刷成洪水）：warning 级 {len(warned)} 条 / 共 {len(hits)} 条"
    )
    assert warned[0]["missing_key"] == "core.hooksPath"
    assert all(e.get("repeat") for e in hits if e.get("log_level") == "debug")


def test_no_degraded_event_when_hooks_dir_available(monkeypatch):
    """反向对照：正常路径不得打降级事件（否则它会变成噪音被忽略）。"""
    import hiveweave.util.win_subprocess as wsub

    monkeypatch.setattr(wsub, "_HARDENING_PAIRS", None)
    monkeypatch.setattr(wsub, "_DEGRADED_WARNED", False)
    monkeypatch.setattr(wsub, "_HOOKS_DIR", "C:/platform/empty-git-hooks")

    with structlog.testing.capture_logs() as logs:
        pairs = wsub._hardening_pairs_cached()

    assert any(k == "core.hooksPath" for k, _ in pairs), pairs
    assert not [e for e in logs if e.get("event") == "git_hardening_degraded"]
