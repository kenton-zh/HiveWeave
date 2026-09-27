"""Pytest 全局夹具。

每个测试结束后关闭该测试期间打开的 aiosqlite 连接（meta DB 单例 +
per-project 连接缓存）。aiosqlite 的连接 worker 线程是**非守护线程**，
不关闭时线程会一直阻塞在队列读取上，导致 pytest 全量单进程跑完汇总后
无法退出（exit hang）。生产进程里这些连接本就该常驻，无需改动 db 层。

会话收尾钩子额外做两件事（治「跑完不退出」的诊断盲区）：
- 取消当前 loop 上遗留的 pending task（game_time tick / inbox watcher /
  offturn job 等测试忘记 stop 的后台协程）；
- 打印残留非守护线程清单 —— 若进程退出仍挂起，最后一段输出直接点名
  元凶线程（aiosqlite worker 名字含连接路径）。
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

# ── 超长环境变量剔除（import 早期执行一次）────────────────────
# Windows putenv 单变量上限 32767 字符。外部工具注入的巨型配置
# （实例：ACC_PRODUCT_CONFIG_V3 ≈ 500KB JSON，某 AI CLI 产品配置）
# 会让所有 ``patch.dict(os.environ, ...)`` 的退出恢复直接 ValueError
# （_unpatch_dict 逐键 setitem 撞上限），污染面是全库任意 environ
# 测试。这类变量测试永不消费，import 时移除即永绝后患。
_ENV_VALUE_HARD_CAP = 32000
_poisoned = [
    k
    for k, v in os.environ.items()
    if len(v) > _ENV_VALUE_HARD_CAP
]
for _k in _poisoned:
    del os.environ[_k]
if _poisoned:
    import sys

    print(
        f"[conftest] dropped oversize env vars (> {_ENV_VALUE_HARD_CAP} "
        f"chars): {_poisoned}",
        file=sys.stderr,
    )


# ── 开发机 .env 与测试解耦（2026-08-30）───────────────────────
# ``hiveweave/__init__.py`` 在包导入时把 apps/hiveweave-py/.env 灌进
# os.environ，于是开发机 .env 的值会渗进测试，而大量用例断言的是**代码
# 默认值** —— 结果随开发机 .env 漂移就失去基线意义。两类实测失败：
#   · 预算类：用例只 delenv 自己 set 过的三个变量后 reload，残留的
#     PACING=600 撞上回到默认的 HARD=570，constants.py 结构 assert 挂掉；
#   · 凭据类：``services/model.py`` 的 Ark 取值链是
#     ``settings.X or os.environ["HIVEWEAVE_ARK_*"]``，os.environ 一有真 key
#     就绕过测试用 FakeSettings 造的空凭据（3 个 model_pool 用例挂掉）。
# 因此在**每个用例前**剥离整个 ``HIVEWEAVE_`` 前缀（不只是预算类），让
# os.environ 回到改动前的干净状态。``settings`` 对象在 config 导入时已固化
# .env 值，不受影响 —— 最终状态与基线一致；需要覆盖的用例在自己作用域内
# monkeypatch.setenv 即可。
_ISOLATED_ENV_PREFIX = "HIVEWEAVE_"


@pytest.fixture(autouse=True)
def _isolate_hiveweave_env(monkeypatch):
    """Strip locally-injected ``HIVEWEAVE_*`` env so tests start from defaults."""
    for key in [k for k in os.environ if k.startswith(_ISOLATED_ENV_PREFIX)]:
        monkeypatch.delenv(key, raising=False)


# ── meta DB 隔离（2026-09-23）──────────────────────────────────
# 病灶：`tests/test_modules_tree.py::_make_project` 与
# `tests/test_memory_sinking.py::_make_project` 都直接
# ``INSERT INTO projects``，而 ``meta_db.init_meta_db()`` 取的是
# ``settings.get_meta_db_path()`` 的**默认值 = 生产库**
# ``apps/hiveweave-py/data/hiveweave.db``；测试结束只 close 连接、
# **从不删行** ⇒ 每跑一次 pytest 就往生产库永久插一行。
# 实测累积到 **1137 行**（"Module Tree Test" 820 + "Memory Sink Test" 312），
# 后果是后端启动被拖到 **~140 秒**（lifespan 要逐个真实项目跑 ACL 哨兵探针）
# 且项目列表被测试垃圾淹没。已于 2026-09-23 手工清理（1192 → 55），
# 本夹具负责「不再长回来」。
#
# ⚠ 为什么不用 ``HIVEWEAVE_META_DB_PATH`` 环境变量：上面那个 autouse 的
#   ``_isolate_hiveweave_env`` 会把**所有** ``HIVEWEAVE_*`` 前缀删掉 ——
#   env 这条通道是堵死的（这正是"设了 env 也不生效"的原因）。
#   所以直接 patch settings **对象本身**：``get_meta_db_path()`` 优先返回
#   ``self.meta_db_path``（config.py 的显式字段最高优先），与 env 无关。
#
# 作用域选 **function**（2026-09-23 独立审计后从 session 改回）：每个用例一个独立
# 临时库，消除「测试写的行被后一个用例读到」这条**同类机制** —— 本次事故正是
# "测试写的一行被产品代码读到"（lifespan 逐项目探针跑到 1192 行）。session 级
# 共享库只是把受害者从"生产启动 140 秒"换成"某个恰好枚举 projects 的用例"，
# 属于隐匿的顺序耦合而不是隔离。
#
# ⚠ 审计纠错：我原先写"session 只建一次表（省几十秒）"——**那是错的**。
#   `_close_db_connections_after_test`（function 级 autouse，见下）每个用例后都会
#   `close_meta_db()` ⇒ `_db = None`、`_migrated = False` ⇒ 下一个碰 meta DB 的
#   用例照样完整重跑 `META_DB_TABLES` + `META_DB_INDEXES` + `_migrate_meta_schema()`。
#   建表开销与作用域**无关**，所以"省建表"从来不是选 session 的理由。
#
# ⚠ 本隔离的**前提条件**：`_close_db_connections_after_test` 必须存在且保持
#   function 级 autouse —— 它保证 `_db` 不跨用例存活（否则会出现"`_db` 指向旧库、
#   settings 指向新库"的世代错配），也保证每用例都是全新库。
#   同理它还是"undo 早于 close"这条顺序的保障。**不要为了省 1000 次 close 把它
#   改成 session 级** —— 那会同时打破两条，症状极难查。
#
# 守卫：`tests/test_meta_db_isolation.py` 断言测试期的 meta 路径不是生产库 ——
#   否则这整个夹具被删/改名都不会有任何用例变红（审计 P1：病灶之所以积到 1137 行，
#   根因就是"在测试里不可观测"）。
@pytest.fixture(autouse=True)
def _isolate_meta_db(tmp_path, monkeypatch):
    """把 meta DB 钉到**本用例**的临时路径 —— 测试绝不写生产库。"""
    from hiveweave.config import settings

    monkeypatch.setattr(settings, "meta_db_path", str(tmp_path / "meta-db.db"))


def _aiosqlite_worker_alive(conn) -> bool:
    """aiosqlite 连接的 worker 线程是否还活着（读私有属性，仅测试清理用）。

    aiosqlite 0.22 的 ``Connection._thread`` 是**非守护** worker，逐条消费
    ``_tx`` 队列并把结果经 ``future.get_loop().call_soon_threadsafe`` 送回。
    """
    t = getattr(conn, "_thread", None)
    return bool(t is not None and t.is_alive())


def _force_stop_aiosqlite_conn(conn) -> None:
    """同步停掉一条 aiosqlite 连接，**绝不 await**。

    为什么不能 await close()：worker 线程已死（它要 resolve 的 future 绑在
    已关闭的旧测试 loop 上）时，``await conn.close()`` 会把 close 操作排进
    一个永远无人消费的队列，future 永不 resolve —— 调用方在
    ``run_until_complete`` 里永久挂起（IocpProactor._poll 无超时阻塞），
    这正是 test_evidence_landing_and_gate_receipts.py 整文件跑必挂死的
    终端表现。这里只入队 stop 哨兵并断开 sqlite 句柄引用（句柄交给 GC），
    语义是「测试进程马上续跑，不需要优雅关闭」。
    """
    try:
        conn.stop()
    except Exception:
        pass
    try:
        if not _aiosqlite_worker_alive(conn):
            conn._connection = None
    except Exception:
        pass


@pytest.fixture(autouse=True)
async def _close_db_connections_after_test():
    yield
    # ── per-project 连接缓存 ──────────────────────────────────
    try:
        from hiveweave.db import project as project_db_mod
        from hiveweave.db.project import close_all

        def _all_cached_conns() -> list:
            conns = list(project_db_mod._cache.values())
            for pool in project_db_mod._readonly_pools.values():
                conns.extend(slot.conn for slot in pool)
            return conns

        cached = _all_cached_conns()
        if cached and not all(_aiosqlite_worker_alive(c) for c in cached):
            # 有 worker 已死的僵尸连接：await close（含 close_all 内部）必挂
            # → 同步止损。（正常路径不会走到这里——每条连接每用例后都被
            # close_all 关掉，worker 不会活过自己的 loop；这是对泄漏路径的
            # 兜底。）
            for c in cached:
                _force_stop_aiosqlite_conn(c)
        else:
            try:
                await close_all()
            except Exception:
                pass
            # close_all 可能半途失败（如某把模块级锁跨 loop RuntimeError）
            # 而留下未关连接 → 兜底止损（对已关连接是幂等 no-op）。
            for c in _all_cached_conns():
                _force_stop_aiosqlite_conn(c)
        # 结束态恒定：任何 per-project 连接/锁都不活过用例边界
        # （与 close_all 的清库语义对齐，含驱逐标记）。
        project_db_mod._cache.clear()
        project_db_mod._agent_cache.clear()
        project_db_mod._write_locks.clear()
        project_db_mod._project_ws_cache.clear()
        project_db_mod._readonly_pools.clear()
        project_db_mod._readonly_rr.clear()
        project_db_mod._evicted_workspaces.clear()
    except Exception:
        pass
    # ── meta DB 单例 ──────────────────────────────────────────
    # 挂根修复（2026-09-26，test_evidence_landing_and_gate_receipts 整文件
    # 跑必挂死）：review 路径 publish_fact → fact_bus._schedule_persist 起的
    # fire-and-forget 落库任务会在 call 阶段拿到 meta._init_lock 开 meta 库；
    # pytest-asyncio 1.4 每个 stage 用 run_until_complete 跑，stage 自己的
    # task 一完成 loop 立即停 —— 这个后台任务冻死在 init 临界区里（持锁）。
    # 下一个用例 teardown 的 close_meta_db 在**旧 loop 绑定的锁**上争用 ⇒
    # RuntimeError("... is bound to a different event loop")，被下面的
    # except 吞掉 ⇒ _db 泄漏进下一用例；其 worker 处理孤儿队列项时对已关
    # loop call_soon_threadsafe ⇒ RuntimeError ⇒ worker 线程死亡；再下一个
    # 用例 teardown await _db.close() ⇒ 死 worker 永不 resolve ⇒ 全进程挂死。
    # 治法 = 不让任何 meta 连接/锁活过用例边界：
    try:
        from hiveweave.db import meta as meta_mod

        db = meta_mod._db
        if db is None or _aiosqlite_worker_alive(db):
            try:
                await meta_mod.close_meta_db()
            except Exception:
                pass
        # close_meta_db 可能因跨 loop 的 _init_lock RuntimeError 整体被跳过
        # （worker 还活着但锁争用失败）：只要 _db 还在就同步止损，
        # 绝不让连接跨用例存活。
        db = getattr(meta_mod, "_db", None)
        if db is not None:
            _force_stop_aiosqlite_conn(db)
            meta_mod._db = None
        meta_mod._migrated = False
        # 幽灵任务可能把 _init_lock 锁死/绑死在已关闭的旧 loop 上 —— 换新锁
        # 根治「bound to a different event loop」。冻结任务最终 release 的
        # 是旧锁对象，与新锁互不干扰。
        meta_mod._init_lock = asyncio.Lock()
        # 同理兜底 project 侧的模块级锁（当前文件实测未绑定，纯保险）。
        from hiveweave.db import project as project_db_mod

        project_db_mod._ensure_lock = asyncio.Lock()
    except Exception:
        pass
    # 兜底：取消本测试 loop 上仍 pending 的后台任务（测试内 start 了
    # game_time / watcher / offturn 却没 stop 的漏网）。loop 即将关闭，
    # task 引用的连接已由上面 close_all 关掉，cancel 语义是纯清理。
    try:
        loop = asyncio.get_running_loop()
        pending = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
        for t in pending:
            t.cancel()
        if pending:
            # 5s 兜底：个别任务可能吞 cancel，不能让清扫自己变挂起点
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True), timeout=5.0
            )
    except (RuntimeError, TimeoutError, asyncio.TimeoutError):
        pass


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """汇总后、解释器退出前的诊断：点名残留非守护线程。

    不阻止挂起（线程已启动不可转 daemon），但把「跑完不退出」从黑盒
    变成有现场线索 —— 挂起时最后一段输出即元凶线程清单。
    """
    import sys

    main = threading.main_thread()
    leftovers = [
        t for t in threading.enumerate()
        if t is not main and t is not threading.current_thread() and not t.daemon
    ]
    if not leftovers:
        return
    print(
        f"\n[teardown] {len(leftovers)} non-daemon thread(s) still alive "
        "(process will hang if they never exit):",
        file=sys.stderr,
    )
    for t in leftovers:
        target = getattr(t, "_target", None)
        name = getattr(target, "__qualname__", "") if target else ""
        print(f"  - {t.name!r} ({t.native_id}) {name}", file=sys.stderr)


class _FakeStdin:
    """Records bytes written; drain/close are no-ops."""

    def __init__(self) -> None:
        self.written = b""

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeStream:
    """Chunked stdout/stderr; ``hang=True`` simulates a daemon that never
    closes its inherited pipe handles (EOF never arrives)."""

    def __init__(self, chunks: list[bytes] | None = None, hang: bool = False) -> None:
        self._chunks = list(chunks or [])
        self._hang = hang

    async def read(self, n: int) -> bytes:
        if self._hang:
            await asyncio.sleep(100)
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProc:
    def __init__(
        self,
        returncode: int = 0,
        out: bytes = b"",
        err: bytes = b"",
        hang_pipes: bool = False,
        wait_sleep: float = 0,
    ) -> None:
        self.returncode = returncode
        self.stdout = _FakeStream([out] if out else [], hang=hang_pipes)
        self.stderr = _FakeStream([err] if err else [], hang=hang_pipes)
        self.stdin = _FakeStdin()
        self._wait_sleep = wait_sleep

    async def wait(self) -> int:
        if self._wait_sleep:
            await asyncio.sleep(self._wait_sleep)
        return self.returncode

    def kill(self) -> None:
        return None


@pytest.fixture
def browse_fake_proc():
    """Fake agent-browser child process wired with the three patches every
    browse subprocess test needs (binary resolution, Windows startupinfo,
    create_subprocess_exec). Configure via the returned context — assignments
    are read live at spawn time, so set them before OR inside the with block:

        with browse_fake_proc as ctx:
            ctx.out = b"ok"
            ...  # ctx.stdin_is_pipe / ctx.stdin_written expose the wiring
    """
    state: dict = {}

    async def fake_exec(*_a, **_k):
        state["kwargs"] = _k
        state["argv"] = [str(x) for x in _a]
        # Read the ctx's CURRENT attributes at spawn time — assignments made
        # inside the `with` block (or before it) both take effect.
        p = _FakeProc(
            returncode=ctx.returncode,
            out=ctx.out,
            err=ctx.err,
            hang_pipes=ctx.hang_pipes,
            wait_sleep=ctx.wait_sleep,
        )
        state["proc"] = p
        return p

    class Ctx:
        returncode = 0
        out: bytes = b""
        err: bytes = b""
        hang_pipes = False
        wait_sleep = 0.0

        @property
        def stdin_arg(self):
            return state.get("kwargs", {}).get("stdin")

        @property
        def stdin_is_pipe(self):
            return self.stdin_arg is asyncio.subprocess.PIPE

        @property
        def spawn_env(self):
            return state.get("kwargs", {}).get("env")

        @property
        def spawn_argv(self) -> list[str]:
            return list(state.get("argv", []))

        @property
        def stdin_written(self) -> bytes:
            proc = state.get("proc")
            return proc.stdin.written if proc is not None else b""

        def __enter__(self):
            self._patches = [
                patch(
                    "hiveweave.tools.browse_tools.resolve_browse_bin",
                    return_value=Path("fake-ab.exe"),
                ),
                patch("asyncio.create_subprocess_exec", new=fake_exec),
            ]
            for m in self._patches:
                m.start()
            return self

        def __exit__(self, *_exc):
            for m in reversed(self._patches):
                m.stop()
            return False

    ctx = Ctx()
    return ctx


# ── #1 治本（2026-09-14）：把 spawn 执行面固定为「原生」的夹具 ──────────
@pytest.fixture
def native_sandbox_plane():
    """本次测试内所有 agent 命令 spawn 判为**原生**执行面。

    为什么需要它：#1 治本后**每一次命令行 spawn 都经唯一判定点**
    （`acl_sandbox.policy.resolve_spawn_decision`）。而 pytest 的 tmp workspace
    往往不满足受限令牌的前置条件（OWNER_RIGHTS-only / 缺 ACL）⇒ 真实受限路径会
    raise `SandboxUnavailableError`，把**与沙箱路由无关**的用例（测 cwd 校验 /
    端口分配 / 注册表语义 / alarm 脚本安全校验）整片带红。

    那些用例对"走哪条执行面"**没有主张**，改造前它们隐式就处在原生路径上
    （那时 dev_server / alarm 根本没接沙箱）⇒ 显式声明原生 = 保持它们的被测语义。

    ⚠ 与沙箱路由**有关**的用例（`test_sandbox_single_entry.py` /
    `test_dev_server_sandbox_wiring.py` / `test_acl_sandbox_*`）**不得**使用本夹具
    —— 那会把被测的东西打桩掉。

    用法：`pytestmark = pytest.mark.usefixtures("native_sandbox_plane")`
    """
    from unittest.mock import AsyncMock

    from hiveweave.services.acl_sandbox import policy

    with patch(
        "hiveweave.services.acl_sandbox.policy.resolve_spawn_decision",
        new=AsyncMock(
            return_value=policy.make_decision(policy.R_NATIVE_CONFIG_OFF)
        ),
    ):
        yield
