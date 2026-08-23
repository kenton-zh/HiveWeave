"""ConfinedRunner —— CreateProcessAsUserW + Job + 排空模型（spec §4.13/§5.4）。

命令分流（§5.4 v3 重设计，活锁根治）：
- 前台命令（timeout 有限或输出预期收敛）：有界排空池，PeekNamedPipe 50ms
  节拍排空 + WaitForSingleObject 超时击杀。长驻命令**永不进有界池**。
- 长驻命令（dev server / unbounded job）：单个全局 watcher 线程轮询全部
  活跃 job（§5.4 的 watcher 线程形态），输出滚动缓冲，**零有界池占用**。

句柄纪律（§4.13 / §5.4 H3）：
- 仅 stdio 子进程端三句柄设 INHERIT；其余句柄一律不设。
- 继承窗口用全局锁严格串行化（§4.13 二选一方案）—— 多 agent 并发 spawn
  时，in-flight 其他命令的可继承子端句柄不得被错误继承（管道 EOF 语义互吞）。
- 失败分支六句柄关闭配对进 try/finally（对齐 DSH "six-close contract"）；
  Job 句柄若被孙进程继承 = 后端死后 job 不销毁、B13 失效。
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from ctypes import wintypes

try:  # pragma: no cover - branch 由平台决定
    import pywintypes
    import win32con
    import win32event
    import win32file
    import win32job
    import win32pipe
    import win32process
    import win32security
except ImportError:  # non-Windows
    pywintypes = None
    win32con = win32event = win32file = win32job = None
    win32pipe = win32process = win32security = None

from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError

CREATE_SUSPENDED = win32con.CREATE_SUSPENDED if win32con is not None else 0
STARTF_USESTDHANDLES = win32con.STARTF_USESTDHANDLES if win32con is not None else 0
HANDLE_FLAG_INHERIT = win32con.HANDLE_FLAG_INHERIT if win32con is not None else 0
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_INFO_CLASS = 9  # JobObjectExtendedLimitInformation


def _require() -> None:
    if win32process is None:
        raise SandboxUnavailableError(
            "ACL sandbox requires Windows (pywin32 unavailable) on this platform"
        )


def _set_inherit(handle, inherit: bool) -> None:
    ctypes.windll.kernel32.SetHandleInformation(
        int(handle), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT if inherit else 0)


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _make_kill_on_close_job():
    """KILL_ON_CLOSE Job（win32job 句柄 + ctypes 设置限制 —— pywin32 未导出）。"""
    job = win32job.CreateJobObject(None, "")
    info = _JOBOBJECT_EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = ctypes.windll.kernel32.SetInformationJobObject(
        int(job), _JOB_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        job.Close()
        raise SandboxUnavailableError(
            "SetInformationJobObject(KILL_ON_JOB_CLOSE) failed",
            api_name="SetInformationJobObject")
    return job


def _drain_pipe(read_handle, buf: list[bytes]) -> None:
    """PeekNamedPipe 非阻塞读尽管道。断管/无数据异常一律吞掉（EOF 语义）。"""
    try:
        while True:
            _hr, total, _ = win32pipe.PeekNamedPipe(read_handle, 0)
            if total == 0:
                return
            _hr, data = win32file.ReadFile(read_handle, total)
            if data:
                buf.append(data)
    except pywintypes.error:
        return


def _close_many(*handles) -> None:
    for h in handles:
        if h is None:
            continue
        try:
            h.Close()
        except Exception:
            pass


class _Spawned:
    __slots__ = ("h_proc", "pid", "job", "out_r", "err_r")

    def __init__(self, h_proc, pid, job, out_r, err_r):
        self.h_proc = h_proc
        self.pid = pid
        self.job = job
        self.out_r = out_r
        self.err_r = err_r

    def close(self) -> None:
        _close_many(self.h_proc, self.job, self.out_r, self.err_r)


# 继承窗口串行化锁（§4.13）：管道创建 → CreateProcessAsUser 之间必须独占。
_SPAWN_INHERIT_LOCK = threading.Lock()


def _spawn_sync(token, command: str, cwd: str, env: dict | None) -> _Spawned:
    """受限 spawn：匿名管道 + CreateProcessAsUserW + KILL_ON_CLOSE Job。

    同步阻塞（微秒级）+ 继承窗口全局串行。子进程以 CREATE_SUSPENDED 启动，
    AssignProcessToJobObject 后 ResumeThread —— 避免子进程抢先 fork 孙进程
    逃逸出 Job。
    """
    _require()
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.bInheritHandle = 1
    with _SPAWN_INHERIT_LOCK:
        out_r, out_w = win32pipe.CreatePipe(sa, 0)
        err_r, err_w = win32pipe.CreatePipe(sa, 0)
        in_r, in_w = win32pipe.CreatePipe(sa, 0)
        _set_inherit(out_w, True)
        _set_inherit(err_w, True)
        _set_inherit(in_r, True)
        _set_inherit(out_r, False)
        _set_inherit(err_r, False)
        _set_inherit(in_w, False)

        job = _make_kill_on_close_job()
        si = win32process.STARTUPINFO()
        si.dwFlags = STARTF_USESTDHANDLES
        si.hStdInput = in_r
        si.hStdOutput = out_w
        si.hStdError = err_w

        h_proc = h_thread = None
        pid: int | None = None
        try:
            h_proc, h_thread, pid, _tid = win32process.CreateProcessAsUser(
                token, None, command, None, None, 1,
                CREATE_SUSPENDED, env, cwd, si)
            # §5.4 H3：进程/线程句柄不设 INHERIT —— 否则孙进程继承后，Job
            # 句柄/进程句柄泄漏（B13 失效面）+ 句柄引用被拉长。
            _set_inherit(h_proc, False)
            _set_inherit(h_thread, False)
            win32job.AssignProcessToJobObject(job, h_proc)
            win32process.ResumeThread(h_thread)
        except Exception as e:
            _close_many(in_r, in_w, out_r, out_w, err_r, err_w,
                        h_thread, h_proc, job)
            raise SandboxUnavailableError(
                f"CreateProcessAsUser failed: {e}",
                api_name="CreateProcessAsUserW")
        # 父进程侧关闭子进程端（EOF 语义）+ 子进程侧读端
        _close_many(in_r, in_w, out_w, err_w, h_thread)
    return _Spawned(h_proc, pid, job, out_r, err_r)


class ConfinedRunner:
    """受限命令执行器：前台有界池 + 长驻全局 watcher。"""

    def __init__(self, max_workers: int = 32):
        self._drain_pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="acl-drain")

    def shutdown(self) -> None:
        self._drain_pool.shutdown(wait=False, cancel_futures=True)

    # ── 前台命令 ────────────────────────────────────────────
    async def run_foreground(
        self, token, command: str, cwd: str, env: dict | None,
        timeout_s: float | None,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._drain_pool,
            self._run_foreground_sync, token, command, cwd, env, timeout_s,
        )

    def _run_foreground_sync(self, token, command, cwd, env, timeout_s) -> dict[str, Any]:
        spawned = _spawn_sync(token, command, cwd, env)
        timed_out = False
        start = time.monotonic()
        out_buf: list[bytes] = []
        err_buf: list[bytes] = []
        try:
            while True:
                rc = win32event.WaitForSingleObject(spawned.h_proc, 50)
                _drain_pipe(spawned.out_r, out_buf)
                _drain_pipe(spawned.err_r, err_buf)
                if rc == win32event.WAIT_OBJECT_0:
                    break
                if timeout_s and time.monotonic() - start > timeout_s:
                    timed_out = True
                    win32job.TerminateJobObject(spawned.job, 1)
                    win32event.WaitForSingleObject(spawned.h_proc, 3000)
                    break
            exit_code = win32process.GetExitCodeProcess(spawned.h_proc)
        finally:
            spawned.close()
        return {
            "exit_code": exit_code,
            "stdout": b"".join(out_buf).decode("utf-8", errors="replace"),
            "stderr": b"".join(err_buf).decode("utf-8", errors="replace"),
            "timed_out": timed_out,
        }

    # ── 长驻命令（dev server / unbounded job） ──────────────
    async def run_long_running(
        self, token, command: str, cwd: str, env: dict | None,
    ) -> "LongRunningJob":
        loop = asyncio.get_running_loop()
        spawned = await asyncio.to_thread(_spawn_sync, token, command, cwd, env)
        job = LongRunningJob(spawned, loop)
        with _ACTIVE_LOCK:
            _ACTIVE.append(job)
        _ensure_watcher()
        return job


# 长驻命令输出缓冲上限（§5.4：滚动保留尾部，防 dev server 持续输出撑爆内存；
# 滚动落盘 job-<id>.log 属 P2，先以内存上限兜底）
_LONG_RUNNING_MAX_BUF = 512 * 1024


class LongRunningJob:
    """单个全局 watcher 线程轮询的活跃 job（零有界池占用）。"""

    def __init__(self, spawned: _Spawned, loop: asyncio.AbstractEventLoop):
        self._spawned = spawned
        self._loop = loop
        self._done = asyncio.Event()
        self._out: list[bytes] = []
        self._err: list[bytes] = []
        self._out_size = 0
        self._err_size = 0
        self._finished = False

    @property
    def pid(self) -> int | None:
        """受限子进程的 OS pid（dev server 注册/杀进程用）。"""
        return self._spawned.pid

    def _append(self, buf: list[bytes], size: int, data: bytes) -> int:
        if data:
            buf.append(data)
            size += len(data)
            # 超限时丢弃最旧块，保尾部
            while size > _LONG_RUNNING_MAX_BUF and len(buf) > 1:
                size -= len(buf.pop(0))
            return size
        return size

    def is_exited(self) -> bool:
        try:
            rc = win32event.WaitForSingleObject(self._spawned.h_proc, 0)
            return rc == win32event.WAIT_OBJECT_0
        except pywintypes.error:
            return True

    def drain(self) -> None:
        chunks_out: list[bytes] = []
        chunks_err: list[bytes] = []
        _drain_pipe(self._spawned.out_r, chunks_out)
        _drain_pipe(self._spawned.err_r, chunks_err)
        for data in chunks_out:
            self._out_size = self._append(self._out, self._out_size, data)
        for data in chunks_err:
            self._err_size = self._append(self._err, self._err_size, data)

    def finalize(self) -> None:
        """watcher 线程调用：最后排空一次 + 通知等待方（线程安全）。"""
        self.drain()
        if self._finished:
            return
        self._finished = True
        try:
            self._loop.call_soon_threadsafe(self._done.set)
        except RuntimeError:
            pass

    async def wait(self, timeout_s: float | None = None) -> bool:
        if timeout_s is not None:
            try:
                await asyncio.wait_for(self._done.wait(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return False
        else:
            await self._done.wait()
        return True

    def terminate(self) -> None:
        try:
            win32job.TerminateJobObject(self._spawned.job, 1)
        except pywintypes.error:
            pass

    def close(self) -> None:
        self._spawned.close()

    def output(self) -> str:
        return b"".join(self._out).decode("utf-8", errors="replace")

    def error_output(self) -> str:
        return b"".join(self._err).decode("utf-8", errors="replace")

    @property
    def exit_code(self) -> int | None:
        if not self._finished:
            return None
        try:
            return win32process.GetExitCodeProcess(self._spawned.h_proc)
        except pywintypes.error:
            return None


_ACTIVE: list[LongRunningJob] = []
_ACTIVE_LOCK = threading.Lock()
_WATCHER_STOP = threading.Event()
_watcher_thread: threading.Thread | None = None
_watcher_started_lock = threading.Lock()


def _ensure_watcher() -> None:
    global _watcher_thread
    with _watcher_started_lock:
        if _watcher_thread is not None and _watcher_thread.is_alive():
            return
        _WATCHER_STOP.clear()
        _watcher_thread = threading.Thread(
            target=_watcher_loop, name="acl-watcher", daemon=True)
        _watcher_thread.start()


def _watcher_loop() -> None:
    while not _WATCHER_STOP.is_set():
        _WATCHER_STOP.wait(0.25)
        if _WATCHER_STOP.is_set():
            break
        done: list[LongRunningJob] = []
        with _ACTIVE_LOCK:
            for job in list(_ACTIVE):
                job.drain()
                if job.is_exited():
                    job.finalize()
                    done.append(job)
            for job in done:
                _ACTIVE.remove(job)


def stop_watcher() -> None:
    """测试/后端退出时调用：停 watcher 并关闭活跃 job（Job 语义全灭）。

    join 旧线程并复位引用 —— 否则 stop 后 ~250ms 内有新 long-running job
    注册时，_ensure_watcher 见旧线程仍 alive 而不起新线程 → 新 job 无人轮询。
    """
    global _watcher_thread
    _WATCHER_STOP.set()
    with _ACTIVE_LOCK:
        jobs = list(_ACTIVE)
        _ACTIVE.clear()
    for job in jobs:
        job.terminate()
        job.finalize()
        job.close()
    with _watcher_started_lock:
        t = _watcher_thread
        _watcher_thread = None
    if t is not None:
        t.join(timeout=2.0)  # daemon 线程 0.25s 节拍，join 很快返回
