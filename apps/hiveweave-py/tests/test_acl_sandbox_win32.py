"""ACL 沙箱 win32 集成测试（spec §12.2/§12.3）—— 真实受限令牌。

仅 Windows 运行（@pytest.mark.win32）；非 Windows 模块级 skip。
覆盖：正路径 / 拒绝路径 / 跨项目隔离 / 兄弟 temp 隔离 / 读不设限 /
Job 超时整树击杀 / 并发 / fail-closed / 自愈 / §4.9 data.db 裁剪。

§4.12 前提：workspace 必须带真实主体 ACE（Python 建的 OWNER_RIGHTS-only
目录对 write-restricted 令牌不可用）—— 夹具显式授予当前用户 SID 写 ACE。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from hiveweave.config import settings
from hiveweave.services.acl_sandbox import SandboxUnavailableError
from hiveweave.services.acl_sandbox import service as svc
from hiveweave.services.acl_sandbox.service import spawn_confined

pytestmark = [pytest.mark.win32]

if not sys.platform.startswith("win"):
    pytest.skip("ACL sandbox win32 integration tests require Windows",
                allow_module_level=True)


@pytest.fixture(scope="session", autouse=True)
def _shutdown_acl_runner():
    """会话结束回收排空池/watcher 线程 —— 非守护线程会阻塞进程退出。"""
    yield
    from hiveweave.services.acl_sandbox.service import shutdown_runner
    from hiveweave.services.acl_sandbox.spawn import stop_watcher

    stop_watcher()
    shutdown_runner()


# ── 公共基础设施 ──────────────────────────────────────────────
COMSPEC = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")


def _dir_has_user_ace(d: Path, ws, user) -> bool:
    try:
        sd = ws.GetNamedSecurityInfo(
            str(d), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION)
    except ws.error:
        return False
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        return False
    for i in range(dacl.GetAceCount()):
        ((t, _f), _m, s) = dacl.GetAce(i)
        if t == ws.ACCESS_ALLOWED_ACE_TYPE and s == user:
            return True
    return False


def _grant_user_ace(d: Path, ws, user) -> None:
    import win32con

    sd = ws.GetNamedSecurityInfo(
        str(d), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        return
    dacl.SetEntriesInAcl([{
        "AccessPermissions": 0x1F01FF,
        "AccessMode": ws.GRANT_ACCESS,
        "Inheritance": win32con.CONTAINER_INHERIT_ACE
        | win32con.OBJECT_INHERIT_ACE,
        "Trustee": {
            "MultipleTrustee": None,
            "MultipleTrusteeOperation": 0,
            "TrusteeForm": ws.TRUSTEE_IS_SID,
            "TrusteeType": ws.TRUSTEE_IS_UNKNOWN,
            "Identifier": user,
        },
    }])
    ws.SetNamedSecurityInfo(
        str(d), ws.SE_FILE_OBJECT, ws.DACL_SECURITY_INFORMATION,
        sd.GetSecurityDescriptorOwner(), sd.GetSecurityDescriptorGroup(),
        dacl, None)


def _ensure_subject_ace(path: Path) -> None:
    """§4.12：给目录授予当前用户 SID 全权（OI/CI）—— 使 write-restricted 过 pass-1。

    除目标目录外，还向上把 OWNER_RIGHTS-only 的祖先目录一并补上 subject ACE：
    pytest 的 tmp_path 链（pytest-of-*/pytest-N/...）是 Python 建的
    OWNER_RIGHTS-only 目录，受限进程经它跑 git 类操作（realpath/mkdir 全路径
    校验）会被中间目录卡死（实测 git init 报 cannot lock ref HEAD）。
    只补到首个已有用户 ACE 的祖先为止，**绝不碰盘根**（C:\\ 等）。
    """
    import win32api
    import win32security as ws

    tok = ws.OpenProcessToken(win32api.GetCurrentProcess(), ws.TOKEN_QUERY)
    user, _ = ws.GetTokenInformation(tok, ws.TokenUser)
    tok.Close()

    if not _dir_has_user_ace(path, ws, user):
        _grant_user_ace(path, ws, user)
    anc = path.parent
    while anc != anc.parent and str(anc).lower() != str(anc.anchor).lower():
        if _dir_has_user_ace(anc, ws, user):
            break
        _grant_user_ace(anc, ws, user)
        anc = anc.parent


@pytest.fixture(autouse=True)
def _sandbox_on(monkeypatch):
    monkeypatch.setattr(settings, "acl_sandbox", True)


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    """带真实主体 ACE 的 workspace（模拟用户常规目录）。"""
    d = tmp_path / "ws"
    d.mkdir(parents=True)
    _ensure_subject_ace(d)
    return d


@pytest.fixture()
def outside(tmp_path: Path) -> Path:
    """带真实主体 ACE 的外部目录（供越界写/读测试）。"""
    d = tmp_path / "outside"
    d.mkdir(parents=True)
    _ensure_subject_ace(d)
    return d


async def _run(ws: Path, agent_id: str, command: str, *,
               timeout_s: float = 60, entry: str = "bash",
               workspace: Path | None = None):
    return await spawn_confined(
        command=command, workdir=str(ws),
        workspace_path=str(workspace or ws),
        agent_id=agent_id, timeout_s=timeout_s, entry=entry)


def _cmd(inner: str) -> str:
    return f'"{COMSPEC}" /c {inner}'


def _prep_worktree(ws: Path) -> None:
    """executor 形态：ws 就是 worktree 根（服务在首命令时补授 ACE）。"""
    (ws / ".hiveweave").mkdir(parents=True, exist_ok=True)


# ── 正路径 ───────────────────────────────────────────────────
async def test_positive_write_inside_workspace(ws: Path) -> None:
    _prep_worktree(ws)
    r = await _run(ws, "A001", _cmd(f"echo data > inside.txt"))
    assert r is not None and r["exit_code"] == 0, r
    assert (ws / "inside.txt").exists()


async def test_pipe_inside_cmd(ws: Path) -> None:
    """§4.5 钉：受限子进程内管道（默认 DACL 注入依赖）。"""
    _prep_worktree(ws)
    (ws / "f.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    r = await _run(ws, "A001", _cmd("type f.txt | findstr alpha"))
    assert r is not None and r["exit_code"] == 0, r
    assert "alpha" in r["stdout"]


async def test_git_workflow_in_worktree(ws: Path) -> None:
    """§4.8 钉：worktree 内 git init/add/commit（git 元数据授权）。"""
    if not shutil.which("git"):
        pytest.skip("git not on PATH")
    _prep_worktree(ws)
    # 用裸 `git`（受限子进程继承 PATH 白名单解析），避免带空格路径
    # 与 cmd /c 引号规则冲突
    r = await _run(ws, "A001", _cmd(
        "git init -q . && echo hi > f.txt && git add f.txt && "
        "git config user.name Test && git config user.email t@t && "
        "git commit -qm test && git log --oneline"))
    assert r is not None and r["exit_code"] == 0, r
    assert (ws / ".git").exists()


async def test_read_outside_unlimited(ws: Path, outside: Path) -> None:
    """读不设限（B1）：workspace 外 cat 读成功。"""
    _prep_worktree(ws)
    (outside / "secret.txt").write_text("readable-content", encoding="utf-8")
    r = await _run(ws, "A001", _cmd(f"type {outside / 'secret.txt'}"))
    assert r is not None and r["exit_code"] == 0, r
    assert "readable-content" in r["stdout"]


async def test_private_temp_writable(ws: Path) -> None:
    """§4.12/§5.4：受限进程可写自己的私有 temp（%TEMP% 重定向到 sandbox-temp）。"""
    _prep_worktree(ws)
    r = await _run(ws, "A001", _cmd("echo t > %TEMP%\\t.txt"))
    assert r is not None and r["exit_code"] == 0, r
    assert (ws / ".hiveweave" / "sandbox-temp" / "A001" / "t.txt").exists()


# ── 拒绝路径 ─────────────────────────────────────────────────
async def test_write_outside_denied(ws: Path, outside: Path) -> None:
    """G1 钉：workspace 外写被内核拒绝（pass-2 落空 → EACCES）。"""
    _prep_worktree(ws)
    target = outside / "evil.txt"
    r = await _run(ws, "A001", _cmd(f"echo pwned > {target}"))
    assert r is not None and r["exit_code"] != 0, r
    assert not target.exists()


async def test_project_root_cannot_write_data_db(ws: Path) -> None:
    """§4.9 钉：项目根角色写 .hiveweave/data.db 被拒（PROTECTED 裁剪）。"""
    (ws / ".hiveweave").mkdir(parents=True, exist_ok=True)
    db = ws / ".hiveweave" / "data.db"
    db.write_text("ORIGINAL", encoding="utf-8")
    # 先跑一条项目根角色命令 → verify-then-skip 铺 grant + 裁剪
    await _run(ws, "CEO", _cmd("echo boot"), entry="bash_main", workspace=ws)
    r = await _run(ws, "CEO", _cmd(f"echo hacked > {db}"), entry="bash_main",
                   workspace=ws)
    assert r is not None and r["exit_code"] != 0, r
    assert db.read_text(encoding="utf-8") == "ORIGINAL"


async def test_bash_main_can_write_project_root(ws: Path) -> None:
    """§5.5 钉：bash_main 在项目根可写（node_modules 等业务目录）。"""
    (ws / ".hiveweave").mkdir(parents=True, exist_ok=True)
    r = await _run(ws, "CEO", _cmd("mkdir node_modules && echo x > node_modules/a"),
                   entry="bash_main", workspace=ws)
    assert r is not None and r["exit_code"] == 0, r
    assert (ws / "node_modules" / "a").exists()


# ── 跨项目 / 兄弟 temp 隔离（G6 内核级用例） ─────────────────
async def test_cross_project_isolation(tmp_path: Path) -> None:
    """项目 A 的受限 token 写项目 B 的 worktree/cache/temp 全被 EACCES 拒。"""
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    for p in (proj_a, proj_b):
        p.mkdir(parents=True)
        _ensure_subject_ace(p)

    # B 先激活：B 的 cache/temp/worktree 铺上 B 的 SID
    (proj_b / ".hiveweave").mkdir(exist_ok=True)
    await _run(proj_b, "B001", _cmd("echo boot"), entry="bash_main", workspace=proj_b)

    # A 激活（获得 A 的 SID 集）
    (proj_a / ".hiveweave").mkdir(exist_ok=True)
    await _run(proj_a, "A001", _cmd("echo boot"), entry="bash_main", workspace=proj_a)

    b_cache = proj_b / ".hiveweave-cache"
    b_temp = proj_b / ".hiveweave" / "sandbox-temp" / "B001"
    targets = [proj_b, b_cache, b_temp]
    for t in targets:
        if not t.exists():
            t.mkdir(parents=True)
        r = await _run(proj_a, "A001", _cmd(f"echo pwned > {t / 'x.txt'}"),
                       entry="bash_main", workspace=proj_a)
        assert r is not None and r["exit_code"] != 0, f"A 应无法写 B: {t}"
        assert not (t / "x.txt").exists()


async def test_sibling_temp_isolation(ws: Path) -> None:
    """兄弟 agent temp 隔离：B 的 temp 目录无 A 的 SID，A 不可写。"""
    _prep_worktree(ws)
    await _run(ws, "A001", _cmd("echo boot"))
    # B 的 temp 目录由平台建好但未授予 B 的 SID（模拟 A 攻击 B 的 temp）
    b_temp = ws / ".hiveweave" / "sandbox-temp" / "B001"
    b_temp.mkdir(parents=True, exist_ok=True)
    _ensure_subject_ace(b_temp)
    r = await _run(ws, "A001", _cmd(f"echo x > {b_temp / 's.txt'}"))
    assert r is not None and r["exit_code"] != 0, r
    assert not (b_temp / "s.txt").exists()


# ── Job / 并发 / 超时 ────────────────────────────────────────
async def test_job_timeout_kills_tree(ws: Path) -> None:
    """§5.4：超时 Job 整树击杀（孙进程死）。"""
    _prep_worktree(ws)
    import time

    t0 = time.monotonic()
    r = await _run(ws, "A001", _cmd("ping -n 30 127.0.0.1 > nul"), timeout_s=3)
    elapsed = time.monotonic() - t0
    assert r is not None and r["timed_out"] is True, r
    assert elapsed < 15, f"Job 未及时整树击杀: {elapsed:.1f}s"


async def test_concurrent_8_commands(ws: Path) -> None:
    """§5.4/§4.13：8 条受限命令并发互不串扰。"""
    _prep_worktree(ws)
    results = await asyncio.gather(*[
        _run(ws, f"A{i:03d}", _cmd(f"echo hi{i} > f{i}.txt")) for i in range(8)
    ])
    for i, r in enumerate(results):
        assert r is not None and r["exit_code"] == 0, r
        assert (ws / f"f{i}.txt").exists()


# ── 自愈（verify-then-skip） ─────────────────────────────────
async def test_self_heal_worktree_recreate(ws: Path) -> None:
    """B-4：worktree 删→同路径重建→下一条写命令成功（不重启后端）。"""
    _prep_worktree(ws)
    await _run(ws, "A001", _cmd("echo one > a.txt"))
    assert (ws / "a.txt").exists()

    # 删除整棵树（模拟懒创建/heal 重建）
    shutil.rmtree(ws)
    ws.mkdir()
    _ensure_subject_ace(ws)
    (ws / ".hiveweave").mkdir(exist_ok=True)

    r = await _run(ws, "A001", _cmd("echo two > b.txt"))
    assert r is not None and r["exit_code"] == 0, r
    assert (ws / "b.txt").exists()


# ── fail-closed ──────────────────────────────────────────────
async def test_fail_closed_on_token_failure(ws: Path, monkeypatch) -> None:
    """M2 靶：token 构造失败 → SandboxUnavailableError，无子进程。"""
    _prep_worktree(ws)

    def _boom(write_sids, temp_sid):
        raise SandboxUnavailableError("mock create fail", api_name="CreateRestrictedToken")

    monkeypatch.setattr(
        "hiveweave.services.acl_sandbox.service.RestrictedTokenFactory.create",
        staticmethod(_boom))
    with pytest.raises(SandboxUnavailableError):
        await _run(ws, "A001", _cmd("echo hi"))


async def test_fail_closed_on_no_subject_ace(ws: Path, monkeypatch) -> None:
    """§4.12：根无真实主体 ACE → fail-closed 拒启用（而非神秘 EACCES）。"""
    _prep_worktree(ws)

    async def _no_ace(path):
        return False

    monkeypatch.setattr(
        "hiveweave.services.acl_sandbox.service._AsyncGrant.has_subject_write_ace_async",
        _no_ace)
    with pytest.raises(SandboxUnavailableError):
        await _run(ws, "A001", _cmd("echo hi"))


# ── 长驻命令（watcher 路径，M8 靶） ──────────────────────────
async def test_sentinel_probes_pass(ws: Path) -> None:
    """P1 §13：各入口哨兵探针（TEMP 标记 + .hiveweave 写拒绝）判据过。"""
    _prep_worktree(ws)
    from hiveweave.services.acl_sandbox.sentinel import run_sentinel_probes

    results = await run_sentinel_probes(str(ws), "sentinel-test")
    for entry, res in results.items():
        if entry == "dev_server":
            continue  # E2E 场景 G 覆盖
        assert res.get("ok") is True, f"{entry} 探针未过: {res}"


async def test_spawn_confined_env_extra_and_pid(ws: Path) -> None:
    """P1：env_extra 注入（dev server PORT）+ long_running 暴露 OS pid。"""
    _prep_worktree(ws)
    from hiveweave.services.acl_sandbox.service import spawn_confined
    from hiveweave.services.acl_sandbox.integration import build_confined_command

    r = await spawn_confined(
        command=build_confined_command("echo $env:PORT"),
        workdir=str(ws), workspace_path=str(ws),
        agent_id="A001", project_id="p1-test",
        entry="bash", env_extra={"PORT": "3999"},
        timeout_s=30,
    )
    assert r is not None and r["exit_code"] == 0, r
    assert "3999" in r["stdout"]

    # long_running：返回 OS pid（dev server 注册用）
    lr = await spawn_confined(
        command=build_confined_command("ping -n 20 127.0.0.1 > nul"),
        workdir=str(ws), workspace_path=str(ws),
        agent_id="A001", project_id="p1-test",
        entry="dev_server", long_running=True,
        timeout_s=None,
    )
    assert lr is not None and lr.get("long_running"), lr
    job = lr["job"]
    assert isinstance(lr.get("pid"), int) and lr["pid"] > 0
    assert job.pid == lr["pid"]
    job.terminate()
    assert await job.wait(timeout_s=5) is True
    job.close()


async def test_long_running_watcher_and_terminate(ws: Path) -> None:
    """§5.4 长驻：起长驻命令（watcher 轮询），terminate 后正常结束。"""
    _prep_worktree(ws)
    from hiveweave.services.acl_sandbox.grant import WriteGrant
    from hiveweave.services.acl_sandbox.service import (
        _AsyncGrant,
        _build_sandbox_env,
        _ensure_runner,
        _ensure_standing_grants,
        _ensure_temp,
    )

    policy = svc.resolve_policy(workspace_path=str(ws), agent_id="A001")
    agrant = _AsyncGrant(WriteGrant())
    await _ensure_standing_grants(policy, agrant)
    await _ensure_temp(policy, agrant)
    factory = svc.RestrictedTokenFactory()
    token = await asyncio.to_thread(factory.create, policy.write_sids, policy.temp_sid)
    try:
        env = _build_sandbox_env(str(ws), policy.cache_dir, policy.temp_dir)
        job = await _ensure_runner().run_long_running(
            token, _cmd("ping -n 30 127.0.0.1 > nul"), str(ws), env)
        await asyncio.sleep(0.5)
        job.terminate()
        assert await job.wait(timeout_s=5) is True
        job.close()
    finally:
        token.Close()


async def test_m8_long_running_does_not_starve_foreground(ws: Path) -> None:
    """M8 靶：33 个长驻命令 + 1 个前台命令，前台须在阈值内完成。

    若长驻误入有界池（默认 32），第 33 个长驻会占满池 → 前台排队直到有
    长驻结束（30s），远超阈值 → 回归即失败。
    """
    _prep_worktree(ws)
    import time
    from hiveweave.services.acl_sandbox.grant import WriteGrant
    from hiveweave.services.acl_sandbox.service import (
        _AsyncGrant,
        _build_sandbox_env,
        _ensure_runner,
        _ensure_standing_grants,
        _ensure_temp,
    )

    policy = svc.resolve_policy(workspace_path=str(ws), agent_id="A001")
    agrant = _AsyncGrant(WriteGrant())
    await _ensure_standing_grants(policy, agrant)
    await _ensure_temp(policy, agrant)
    factory = svc.RestrictedTokenFactory()
    token = await asyncio.to_thread(factory.create, policy.write_sids, policy.temp_sid)
    try:
        env = _build_sandbox_env(str(ws), policy.cache_dir, policy.temp_dir)
        runner = _ensure_runner()
        jobs = []
        try:
            for _ in range(33):
                jobs.append(await runner.run_long_running(
                    token, _cmd("ping -n 30 127.0.0.1 > nul"), str(ws), env))
            t0 = time.monotonic()
            fg = await runner.run_foreground(token, _cmd("echo fast"), str(ws), env, 30)
            elapsed = time.monotonic() - t0
            assert fg["exit_code"] == 0, fg
            assert elapsed < 15, f"前台被长驻饿死: {elapsed:.1f}s"
        finally:
            for j in jobs:
                j.terminate()
    finally:
        token.Close()
