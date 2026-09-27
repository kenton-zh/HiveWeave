"""卷余量护栏（I1 / 报告 P0-0，2026-09-27）。

## 为什么需要它（实测事故）

s3-clone_13：叶子 agent 在 worktree 里跑 ``python -m unittest discover -s tests -v``，
被测程序里的一条**无界流式上传**（payload ``abcdefghij`` 无限循环）把宿主 D 盘
写掉 **188 GB** 到 ENOSPC。后果不是「那个 agent 失败」，而是**平台自己也写不进库** ——
``llm_usage`` 从 09:35:51 到 11:07:37 **92 分钟零写入**，事后才批量补写。

## 为什么现役上限拦不住

平台**已有**工具输出上限（``tools/executor.py`` 的 ``TOOL_OUTPUT_MAX_BYTES=50_000``
/ ``TOOL_OUTPUT_FILE_MAX_BYTES=10MB``），但它们只盖**平台自己写盘**那一段；
本次写入者是 ``unittest discover`` 拉起的**被测程序**，不经过任何平台写入点 ⇒ 零闸。

上游同族也一样：opencode 的 ``max_bytes`` / DSH 的 ``maxInlineTokens`` 都装在
「平台写盘 → 上下文」这一段，**没有一家对「子进程自由写盘」设闸** —— 所以本条
不是「通用平台都该有」，而是 **HiveWeave 自己制造了耦合**（平台本体与 agent
工作区同一块卷 ⇒ 子进程写满盘，杀的是平台自己）。

## 结构上的根治 vs 应用层的兜底

首选是**消耦**（把 agent 工作区 / 临时写盘位与平台本体数据卷分开）—— 那是部署
形态，本模块不解决它。本模块是**应用层兜底**：即使仍同卷，也不让**一条命令**
把整卷吃掉。

## 三层 + 三个正交事实位

* ① :func:`precheck` —— 命令启动前的余量预检（低于下限 ⇒ 拒跑，附处方）
* ② :func:`watch`    —— 运行中余量监控（低于危险线 ⇒ 杀进程树）
* ③ :func:`finish`   —— 取回三个**正交**事实位

⚠ **三件事必须正交**（DSH ``docs/defensive-patterns.md:7-9`` 的戒律：
"never nest one flag's report inside another's branch"）：

* 「写了多少字节」≠「是否触顶」（可能没触顶，却已写了 50 GB）；
* 「触顶了」≠「进程树已回收」（kill 请求发出 ≠ 树已经没了）。

把三者嵌进一个标志 ⇒ 调用方会把「被截断的 run」读成「干净成功」。

⚠ **``bytes_written`` 是卷级近似**：用「同卷可用空间的下降量」估，**不是**对被测
进程的精确记账（同卷其他写入者会混入，读取间隔内的噪声也会有）。本仓纪律：
估算必须标成估算，不许当实计 —— 需要精确账时看 ``free_at_start`` /
``free_at_end`` 原始读数。

## 只挂一处

接入点是 ``util/win_subprocess.py`` 的 4 个 ``hidden_*`` —— 那是全平台子进程的
**唯一漏斗**（本仓纪律：机制挂唯一出口，不逐点替换 ⇒ 否则必然漏点）。
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import structlog

log = structlog.get_logger(__name__)

# ── 阈值（GiB 口径）─────────────────────────────────────────
#
# 两个阈值**不能合并**：预检是「一开始就没空间就别起进程」，危险线是
# 「跑到一半把整卷吃掉」——后者才是本次事故的形态（预检通过后仍可能暴涨）。
#: 启动前余量下限：低于它就拒跑（保守，4 GiB 足够任何正常命令起步）。
DEFAULT_MIN_FREE_BYTES = 4 * 1024**3
#: 运行中危险线：低于它就杀进程树（1 GiB 时平台自己已经开始写不进了）。
DEFAULT_DANGER_FREE_BYTES = 1 * 1024**3
#: 轮询间隔（秒）。2 s 对 188 GB 级暴涨来说仍可能晚 —— 但它是「减少损失」，
#: 不是「零损失」；真正的根治是分卷（见模块 docstring）。
DEFAULT_CHECK_INTERVAL_S = 2.0
#: kill 后等进程树消失的上限（秒）。超过就如实报 ``tree_reaped=False``。
DEFAULT_REAP_TIMEOUT_S = 10.0


class DiskPressureError(OSError):
    """卷余量不足 ⇒ 命令**从未启动**（不是「跑了但失败」）。

    继承 :class:`OSError` 是**故意的**：shell 工具的执行点已经对 ``OSError``
    有出口（落 ``runner_failed`` = 「命令没跑起来」这一格），语义正好吻合。
    这样本条不需要在调用链上新增 except 分支就落在正确的格子里。
    """


def denial_flags(exc: BaseException) -> dict[str, Any]:
    """工具层用：把「卷预检拒绝」翻成**拒因事实位**（不是该异常 ⇒ 空 dict）。

    ⚠ **只给 `denied_by`，不给 `blocked_by_environment`**（2026-09-27 审计必修）。
    该列在本仓有明确定义且**恰好在讲「runner 有没有跑起来」**：
    `db/schema.py` 的注释是「拒绝来自环境（ACL/封条）**而非 runner 没跑起来**」，
    实现层判据是 `exit_code is not None`（进程**确实跑过**，见
    `services/acl_sandbox/service.py` 的 `_maybe_append_rejection_hint`）。
    而 ``DiskPressureError`` 的语义恰恰是**命令从未启动** ⇒ 标 1 会把该列
    设计用来区分的两种情形**又合并**（P0-2「同一标志两个含义」的复发）。

    处方也不同：ACL 类是「换落点 / 申请豁免」，磁盘类是「清理该卷 / 换卷」。
    """
    if isinstance(exc, DiskPressureError):
        return {"denied_by": "disk_pressure"}
    return {}


@dataclass(frozen=True)
class VolumeReading:
    """一次卷读数（原始值，不做任何推断）。"""

    root: str
    free: int
    total: int

    @property
    def free_gib(self) -> float:
        return self.free / (1024**3)


@dataclass
class DiskFacts:
    """一条命令的卷事实（三个正交位 + 原始读数）。

    ``None`` 一律表示「**未观测**」（没起监控 / kill 后没等到树消失），
    与 ``False``（**观测到否**）不同形 —— 本仓纪律：不许用同一个值同时
    表示「没有」和「假」。
    """

    volume: str
    free_at_start: int | None = None
    free_at_end: int | None = None
    free_at_trip: int | None = None
    #: ① 写入量级（卷级近似，见模块 docstring）
    bytes_written: int | None = None
    #: ② 是否触顶（低于危险线并被强制中止）
    hit_limit: bool | None = None
    #: ③ 进程树是否**已确认**回收（kill 请求 ≠ 回收完成）
    tree_reaped: bool | None = None
    watched: bool = False
    trip_at_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "volume": self.volume,
            "free_at_start": self.free_at_start,
            "free_at_end": self.free_at_end,
            "free_at_trip": self.free_at_trip,
            "bytes_written": self.bytes_written,
            "hit_limit": self.hit_limit,
            "tree_reaped": self.tree_reaped,
            "watched": self.watched,
            "trip_at_ms": self.trip_at_ms,
        }


# ── 卷解析与读数 ────────────────────────────────────────────


def volume_root(path: str | os.PathLike[str]) -> str:
    """路径所在**卷根**（Windows: ``D:\\``；POSIX: ``/``）。

    用 ``Path.anchor`` 而不是自己切字符串：它认得 ``D:\\`` / ``\\\\server\\share\\``
    / ``/`` 三种形态，手写切法在 UNC 与相对路径上会错。
    """
    p = Path(path)
    try:
        resolved = p.resolve()
    except OSError:  # 路径还不存在（新建工作区）⇒ 退回绝对化
        resolved = Path(os.path.abspath(str(p)))
    anchor = resolved.anchor
    return anchor or os.sep


def read_volume(path: str | os.PathLike[str]) -> VolumeReading:
    """读该路径所在卷的可用/总容量。"""
    root = volume_root(path)
    usage = shutil.disk_usage(root)
    return VolumeReading(root=root, free=usage.free, total=usage.total)


def precheck(
    path: str | os.PathLike[str],
    *,
    min_free_bytes: int | None = None,
) -> tuple[bool, VolumeReading | None, str]:
    """① 启动前预检。

    返回 ``(allowed, reading, remedy)``：

    * ``allowed=True`` ⇒ ``remedy`` 为空，调用方照常起进程；
    * ``allowed=False`` ⇒ ``remedy`` 是**给人看的处方**（本仓纪律：拒绝必带处方）。

    读卷失败（路径/权限异常）⇒ **放行**并留日志：预检是护栏，不是新的失败源
    —— 不能因为量不出余量就把所有命令拒掉（那会把一个 P0 换成另一个 P0）。
    """
    limit = DEFAULT_MIN_FREE_BYTES if min_free_bytes is None else min_free_bytes
    try:
        reading = read_volume(path)
    except OSError as exc:
        log.warning("disk_guard.precheck_unavailable", path=str(path), error=str(exc))
        return True, None, ""
    if reading.free >= limit:
        return True, reading, ""
    remedy = (
        f"卷 {reading.root} 可用空间 {reading.free_gib:.1f} GiB，低于预检下限 "
        f"{limit / (1024**3):.1f} GiB ⇒ 命令未启动（避免写到 ENOSPC 把平台自己也拖死）。"
        "处方：① 清理该卷空间后重试；② 或把工作区/数据根挪到别的卷"
        "（项目 workspacePath / env HIVEWEAVE_DATA_ROOT）。"
    )
    log.warning(
        "disk_guard.precheck_refused",
        volume=reading.root,
        free_bytes=reading.free,
        limit_bytes=limit,
    )
    return False, reading, remedy


# ── 进程抽象（sync Popen 与 async Process 共用）──────────────


def _proc_pid(proc: Any) -> int | None:
    pid = getattr(proc, "pid", None)
    return int(pid) if isinstance(pid, int) else None


def _proc_alive(proc: Any) -> bool:
    """进程（顶层）是否仍活着。``poll()``（Popen）/ ``returncode``（asyncio）两者都认。"""
    poll = getattr(proc, "poll", None)
    if callable(poll):
        try:
            return poll() is None
        except Exception:  # noqa: BLE001 —— 判活失败按「已退出」处理（保守：不 kill）
            return False
    rc = getattr(proc, "returncode", None)
    return rc is None


def _session_alive(session: "_Session") -> bool:
    """会话的存活判据：**优先用调用方注入的 `is_alive`**。

    为什么需要注入：受限 spawn（``acl_sandbox`` 的 ``CreateProcessAsUserW``）
    返回的既不是 ``Popen`` 也不是 ``asyncio.Process``，而是带 ``h_proc`` 的
    句柄包装 —— 上面那套 ``poll()/returncode`` 判据对它一律返回「已退出」，
    监控会**静默失效**（而且失效方向是「以为死了」⇒ 不再巡检查卷）。
    """
    if session.is_alive is not None:
        try:
            return bool(session.is_alive())
        except Exception:  # noqa: BLE001 —— 判活失败按「已退出」处理
            return False
    return _proc_alive(session.proc)


def _session_pid(session: "_Session") -> int | None:
    """会话的 pid：优先用调用方注入的（受限侧从 `_Spawned.pid` 取）。"""
    if session.pid is not None:
        return session.pid
    return _proc_pid(session.proc)


def _kill_tree(pid: int) -> None:
    """杀**进程树**（不是只杀顶层）。

    本次事故的写入者是 ``unittest discover`` 的**子进程**：只杀顶层 shell 会留下
    正在写盘的孙进程继续吃卷。

    ⚠ 走 ``hidden_run(_disk_guard=False)`` 而**不是**裸 ``subprocess``：
    ① 本仓 ``tests/test_spawn_funnel_guard.py`` 禁止漏斗外直接 spawn；
    ② 必须显式关掉卷守卫 —— 否则 ``taskkill`` 自己会被登记成一个会话，
    而它正好在「卷已满」的时刻启动 ⇒ 必然立刻触顶、递归触发 kill。
    """
    if pid <= 0:
        return
    try:
        if os.name == "nt":
            from hiveweave.util.win_subprocess import hidden_run

            hidden_run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
                timeout=DEFAULT_REAP_TIMEOUT_S,
                _disk_guard=False,
            )
        else:
            import signal

            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception as exc:  # noqa: BLE001 —— kill 失败要留痕，但不能让监控线程死掉
        log.warning("disk_guard.kill_failed", pid=pid, error=str(exc))


# ── 会话与全局监控线程 ──────────────────────────────────────


@dataclass
class _Session:
    proc: Any
    path: str
    volume: str
    danger_free: int
    facts: DiskFacts
    interval_s: float = DEFAULT_CHECK_INTERVAL_S
    #: 调用方注入的存活判据 / pid（受限 spawn 的句柄包装不支持 poll()/returncode）。
    is_alive: Callable[[], bool] | None = None
    pid: int | None = None
    _tripped: bool = field(default=False)


_sessions: dict[int, _Session] = {}
_sessions_lock = threading.Lock()
_watcher: threading.Thread | None = None
_watcher_lock = threading.Lock()
_stop = threading.Event()


def _trip(session: _Session, reading: VolumeReading) -> None:
    """触顶：先记账，再杀树，最后**验证**树是否真的没了。"""
    pid = _session_pid(session)
    session._tripped = True
    session.facts.hit_limit = True
    session.facts.free_at_trip = reading.free
    session.facts.trip_at_ms = int(time.time() * 1000)
    log.warning(
        "disk_guard.trip",
        volume=reading.root,
        free_bytes=reading.free,
        danger_free_bytes=session.danger_free,
        pid=pid,
        path=session.path,
    )
    if pid is not None:
        _kill_tree(pid)
        session.facts.tree_reaped = _await_reaped(lambda: _session_alive(session))
    else:
        # 拿不到 pid ⇒ **不许猜**（记 None = 未观测，而不是 False）
        session.facts.tree_reaped = None


def _await_reaped(
    is_alive: Callable[[], bool], *, timeout_s: float = DEFAULT_REAP_TIMEOUT_S
) -> bool:
    """kill 请求发出 ≠ 树没了 —— 轮询到确认，或如实报否。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not is_alive():
            return True
        time.sleep(0.2)
    return not is_alive()


def _watch_loop() -> None:
    """全局单轮询线程：**按卷去重**查询，一个卷每次周期只读一次盘。

    ⚠ 等待时长取**当前所有会话里最短的那个 interval**，不用模块级全局值 ——
    否则「一条命令的短周期」会永久改变全平台巡检节奏（审计 ②-6）。
    """
    while not _stop.is_set():
        with _sessions_lock:
            sessions = list(_sessions.values())
        if not sessions:
            _stop.wait(DEFAULT_CHECK_INTERVAL_S)
            continue
        _stop.wait(min(s.interval_s for s in sessions))
        by_volume: dict[str, VolumeReading | None] = {}
        for session in sessions:
            # ⚠ 顺序要紧：**先判存活、后判已触顶**。反过来的话，触顶会话会被
            # `continue` 跳过 ⇒ 它**永不被回收**（审计 ①-3 实测的泄漏：
            # `_Session` 强引用着 proc，会话表随每次触顶增长）。
            if not _session_alive(session):
                # 进程已退出 ⇒ 自动收尾（补 free_at_end、注销会话）。
                # facts 对象由调用方持有（挂在 proc 上），收尾不影响读取。
                finish(session.proc)
                continue
            if session._tripped:
                continue
            if session.volume not in by_volume:
                try:
                    by_volume[session.volume] = read_volume(session.volume)
                except OSError:
                    by_volume[session.volume] = None
            reading = by_volume[session.volume]
            if reading is None:
                continue
            if reading.free < session.danger_free:
                _trip(session, reading)


def _ensure_watcher() -> None:
    global _watcher
    with _watcher_lock:
        if _watcher is not None and _watcher.is_alive():
            return
        _stop.clear()
        _watcher = threading.Thread(
            target=_watch_loop, name="disk-guard-watcher", daemon=True
        )
        _watcher.start()


#: 事实对象挂到 ``proc`` 上的属性名。用属性而不是「按 id 查表」取回事实：
#: 调用方（工具层）只拿得到 ``proc``，而地址做查询键是本仓明令禁止的形态。
_FACTS_ATTR = "_hiveweave_disk_facts"


def _attach(proc: Any, facts: DiskFacts) -> None:
    """把事实挂到进程对象上（失败不致命：某些对象可能禁止设属性）。"""
    try:
        setattr(proc, _FACTS_ATTR, facts)
    except Exception:  # noqa: BLE001
        log.warning("disk_guard.attach_failed", proc=type(proc).__name__)


def facts_of(proc: Any) -> DiskFacts | None:
    """取回挂在 ``proc`` 上的事实（未挂过 ⇒ ``None``）。"""
    return getattr(proc, _FACTS_ATTR, None)


def watch(
    proc: Any,
    path: str | os.PathLike[str],
    *,
    danger_free_bytes: int | None = None,
    interval_s: float | None = None,
    is_alive: Callable[[], bool] | None = None,
    pid: int | None = None,
) -> DiskFacts:
    """② 给一条刚起来的命令挂监控。

    返回的 :class:`DiskFacts` **同时挂到 ``proc`` 上**（见 :func:`facts_of`）⇒
    调用方在命令结束后用 :func:`finish` 取回，不需要额外句柄传递。

    ``is_alive`` / ``pid``：**受限 spawn 必须传**。``acl_sandbox`` 的
    ``_Spawned`` 既没有 ``poll()`` 也没有 ``returncode``，不传就会被判成
    「已退出」⇒ 监控静默失效（失效方向还是「以为死了」）。受限侧传
    ``is_alive=lambda: WaitForSingleObject(h_proc, 0) != WAIT_OBJECT_0``
    与 ``pid=spawned.pid``。

    读卷失败（路径/权限）⇒ 记 ``watched=False`` 后返回，**不拦命令**（护栏不得
    变成新的失败源）。拿不到 pid 也登记（用于读数），``tree_reaped`` 留 ``None``。
    """
    danger = (
        DEFAULT_DANGER_FREE_BYTES if danger_free_bytes is None else danger_free_bytes
    )
    facts = DiskFacts(volume=volume_root(path), watched=True)
    try:
        facts.free_at_start = read_volume(path).free
    except OSError:
        facts.watched = False
        return facts
    session = _Session(
        proc=proc,
        path=str(path),
        volume=facts.volume,
        danger_free=danger,
        facts=facts,
        # ⚠ interval 存**会话**而非模块级全局：否则「一条命令传了短周期」
        # 会永久改变全平台巡检节奏（审计 ②-6）。
        interval_s=interval_s if (interval_s or 0) > 0 else DEFAULT_CHECK_INTERVAL_S,
        is_alive=is_alive,
        pid=pid,
    )
    _attach(proc, facts)
    # ⚠ ``id(proc)`` 只作**内部**索引：``_sessions`` 持有 proc 强引用 ⇒ 该对象
    # 不会被 GC ⇒ id 不可能被别的对象复用（本仓禁用地址判据，正是怕这种复用）。
    with _sessions_lock:
        _sessions[id(proc)] = session
    _ensure_watcher()
    return facts


def finish(proc: Any) -> DiskFacts | None:
    """③ 会话注销 + 事实位补全（**幂等**；未挂过监控 ⇒ ``None``）。

    ``bytes_written`` 只在**能确定同卷起止读数**时才给值：读不到就留 ``None``
    （未观测），绝不用 0 冒充「没写」。

    幂等是必需的：watcher 对已退出的进程会自动收尾，调用方事后再调一次必须安全
    —— 因此本函数**不依赖会话表**，而是从 ``proc`` 上取 facts（见 :func:`facts_of`）。

    ⚠ 幂等**覆盖事实值**，不只覆盖会话注销（审计 ②-3）：终读数只在 ``None`` 时
    采一次（首次观测为准）。否则 watcher 收尾后再调一次会**重读**——若两次之间
    卷被清理，`bytes_written` 会被 `max(0, ·)` **归零**，静默丢掉「确实写过 N GB」。
    """
    facts = facts_of(proc)
    if facts is None:
        return None
    with _sessions_lock:
        _sessions.pop(id(proc), None)
    if facts.free_at_end is None:
        try:
            facts.free_at_end = read_volume(facts.volume).free
        except OSError:
            facts.free_at_end = None
    if (
        facts.bytes_written is None
        and facts.free_at_start is not None
        and facts.free_at_end is not None
    ):
        facts.bytes_written = max(0, facts.free_at_start - facts.free_at_end)
    if facts.hit_limit is None:
        # 观测了整段且没触发 ⇒ 明确记 False（与 None = 未观测 不同形）
        facts.hit_limit = False
    return facts


def reset_for_tests() -> None:
    """测试夹具用：停线程、清会话（interval 已随会话，不需要单独还原）。"""
    global _watcher
    _stop.set()
    with _watcher_lock:
        thread = _watcher
        _watcher = None
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2.0)
    with _sessions_lock:
        _sessions.clear()
    _stop.clear()


def active_sessions() -> int:
    """当前在监控的会话数（测试与观测用）。"""
    with _sessions_lock:
        return len(_sessions)


__all__ = [
    "DEFAULT_CHECK_INTERVAL_S",
    "DEFAULT_DANGER_FREE_BYTES",
    "DEFAULT_MIN_FREE_BYTES",
    "DEFAULT_REAP_TIMEOUT_S",
    "DiskFacts",
    "DiskPressureError",
    "VolumeReading",
    "active_sessions",
    "denial_flags",
    "facts_of",
    "finish",
    "precheck",
    "read_volume",
    "reset_for_tests",
    "volume_root",
    "watch",
]
