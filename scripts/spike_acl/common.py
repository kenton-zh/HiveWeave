"""ACL 沙箱 spike 公共基础设施（spec docs/spec/windows-acl-sandbox.md §10.1）。

一次性验证代码，不进 src/。涵盖 S3/S4/S6/S7 的 API 形态验证 + 受限 spawn。
对照 DSH packages/sandbox/sandbox-windows-acl/src/{token,spawn,acl,grant}.ts。

pywin32 实测形态（probe_api.py / probe_api2.py 确认）：
- SID 字符串化用 win32security.ConvertSidToStringSid（str() 带 PySID: 前缀）
- GetNamedSecurityInfo → PySECURITY_DESCRIPTOR；.GetSecurityDescriptorDacl() 取 ACL
- PyACL.GetAce(i) → ((ace_type, ace_flags), mask, sid)；mask 可能负数须 & 0xFFFFFFFF
- PyACL.SetEntriesInAcl([{AccessPermissions, AccessMode, Inheritance, Trustee:dict}])
  原地修改并返回 None（Trustee dict 五键：MultipleTrustee/MultipleTrusteeOperation/
  TrusteeForm/TrusteeType/Identifier）
- CreateRestrictedToken restrict_sids 需要 [(PySID, attr)] 元组列表
"""

from __future__ import annotations

import hashlib
import os
import time

import win32api
import win32con
import win32event
import win32file
import win32job
import win32pipe
import win32process
import win32security
import pywintypes

# ── 常量（DSH win32-abi.ts 对齐；pywin32 未导出部分） ──────────────────
DISABLE_MAX_PRIVILEGE = 0x1
LUA_TOKEN = 0x4
WRITE_RESTRICTED = 0x8
TOKEN_FLAGS = DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED

TOKEN_ALL_NEED = (
    win32security.TOKEN_QUERY
    | win32security.TOKEN_DUPLICATE
    | win32security.TOKEN_ADJUST_DEFAULT
    | win32security.TOKEN_ASSIGN_PRIMARY
)

READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
STANDARD_RIGHTS_WRITE = READ_CONTROL | WRITE_DAC | WRITE_OWNER        # 0x000E0000
DELETE = 0x00010000
FILE_DELETE_CHILD = 0x00000040
FILE_GENERIC_WRITE = READ_CONTROL | 0x2 | 0x4 | 0x10 | 0x100 | 0x100000
GRANT_MASK = (FILE_GENERIC_WRITE | DELETE | FILE_DELETE_CHILD) & ~STANDARD_RIGHTS_WRITE  # 0x110156
CACHE_MASK = (FILE_GENERIC_WRITE & ~DELETE & ~FILE_DELETE_CHILD) & ~STANDARD_RIGHTS_WRITE

CREATE_SUSPENDED = win32con.CREATE_SUSPENDED
STARTF_USESTDHANDLES = win32con.STARTF_USESTDHANDLES
HANDLE_FLAG_INHERIT = win32con.HANDLE_FLAG_INHERIT
SE_GROUP_LOGON_ID = 0xC0000000
OI_CI = win32con.CONTAINER_INHERIT_ACE | win32con.OBJECT_INHERIT_ACE
ACE_ALLOWED = win32con.ACCESS_ALLOWED_ACE_TYPE


def sid_str(sid) -> str:
    return win32security.ConvertSidToStringSid(sid)


# ── SID 派生（spec §4.3） ─────────────────────────────────────────────
def _digest_sid(prefix: str, path: str, extra: tuple[int, ...] = ()) -> str:
    d = hashlib.sha256((prefix + "\0" + path).encode("utf-8")).digest()
    a = int.from_bytes(d[0:4], "little") % (2**30 - 1) + 1
    b = int.from_bytes(d[4:8], "little") % (2**30 - 1) + 1
    return "S-1-4-" + "-".join(str(x) for x in (a, b, *extra))


def workspace_write_sid(workspace_root: str) -> str:
    return _digest_sid("", os.path.realpath(workspace_root))


def temp_write_sid(temp_dir: str) -> str:
    return _digest_sid("temp", os.path.realpath(temp_dir), (1,))


# ── token 工厂（spec §5.3；对照 DSH token.ts） ────────────────────────
def open_current_process_token():
    return win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), TOKEN_ALL_NEED)


def find_logon_sid(token):
    groups = win32security.GetTokenInformation(token, win32security.TokenGroups)
    for sid, attr in groups:
        if (attr & SE_GROUP_LOGON_ID) == SE_GROUP_LOGON_ID:
            return sid
    raise RuntimeError("no logon SID in token groups")


def _trustee(sid):
    return {
        "MultipleTrustee": None,
        "MultipleTrusteeOperation": 0,
        "TrusteeForm": win32security.TRUSTEE_IS_SID,
        "TrusteeType": win32security.TRUSTEE_IS_UNKNOWN,
        "Identifier": sid,
    }


def _explicit_access(sid, mask: int, mode: int, inheritance: int) -> dict:
    return {
        "AccessPermissions": mask,
        "AccessMode": mode,
        "Inheritance": inheritance,
        "Trustee": _trustee(sid),
    }


def create_restricted_token(write_sids: list, temp_sid):
    """WRITE_RESTRICTED 令牌 + 默认 DACL 注入（spec §4.5）。fail-closed。"""
    current = open_current_process_token()
    try:
        logon = find_logon_sid(current)
        everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid)
        restricting = [(s, 0) for s in (logon, everyone, *write_sids, temp_sid)]
        restricted = win32security.CreateRestrictedToken(
            current, TOKEN_FLAGS, [], [], restricting)
    finally:
        current.Close()

    default_dacl = win32security.GetTokenInformation(
        restricted, win32security.TokenDefaultDacl)
    inject = temp_sid if temp_sid is not None else (
        write_sids[0] if write_sids else everyone)
    default_dacl.SetEntriesInAcl([_explicit_access(
        inject, 0x1F01FF, win32security.GRANT_ACCESS, 0)])
    win32security.SetTokenInformation(
        restricted, win32security.TokenDefaultDacl, default_dacl)
    return restricted, inject


# ── grant（spec §4.4；对照 DSH acl.ts 精确 ACE 跳过） ─────────────────
def _iter_aces(dacl):
    """PyACL → (type, flags, mask_unsigned, sid_str)。"""
    for i in range(dacl.GetAceCount()):
        (ace_type, ace_flags), mask, s = dacl.GetAce(i)
        yield ace_type, ace_flags, mask & 0xFFFFFFFF, sid_str(s)


def ace_of(dacl, want_sid_str: str, want_mask: int):
    for ace_type, ace_flags, mask, s in _iter_aces(dacl):
        if s == want_sid_str and mask == want_mask:
            return ace_type, ace_flags, mask, s
    return None


def grant_write(path: str, sid_string: str, mask: int = GRANT_MASK) -> bool:
    """授予能力 SID 写 ACE（目录 OI/CI 继承），精确 ACE 跳过。True=实际写入。"""
    sd = win32security.GetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    flags = OI_CI if os.path.isdir(path) else 0
    for ace_type, ace_flags, m, s in _iter_aces(dacl):
        if (s == sid_string and m == mask
                and (flags == 0 or ace_flags & OI_CI == OI_CI)
                and ace_type == ACE_ALLOWED):
            return False  # 精确跳过
    sid = win32security.ConvertStringSidToSid(sid_string)
    dacl.SetEntriesInAcl([_explicit_access(
        sid, mask, win32security.GRANT_ACCESS, flags)])
    win32security.SetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        sd.GetSecurityDescriptorOwner(), sd.GetSecurityDescriptorGroup(),
        dacl, None)
    return True


def revoke_write(path: str, sid_string: str) -> None:
    sd = win32security.GetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    sid = win32security.ConvertStringSidToSid(sid_string)
    dacl.SetEntriesInAcl([_explicit_access(
        sid, 0, win32security.REVOKE_ACCESS, 0)])
    win32security.SetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        sd.GetSecurityDescriptorOwner(), sd.GetSecurityDescriptorGroup(),
        dacl, None)


# ── Job 对象（ctypes 直调；pywin32 未导出扩展限制结构） ────────────────
import ctypes
from ctypes import wintypes

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9


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


def make_kill_on_close_job():
    """KILL_ON_JOB_CLOSE Job 对象（win32job 句柄 + ctypes 设置限制）。"""
    job = win32job.CreateJobObject(None, "")
    info = _JOBOBJECT_EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = ctypes.windll.kernel32.SetInformationJobObject(
        int(job), JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        job.Close()
        raise OSError("SetInformationJobObject failed")
    return job


# ── 受限 spawn（spec §5.4；对照 DSH spawn.ts） ────────────────────────
def _set_inherit(handle, inherit: bool) -> None:
    ctypes.windll.kernel32.SetHandleInformation(
        int(handle), HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT if inherit else 0)


def confined_spawn(token, command: str, cwd: str, env: dict | None = None,
                   timeout_s: float = 60.0):
    """受限 spawn + 匿名管道捕获 + KILL_ON_CLOSE Job + 超时整树击杀。"""
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.bInheritHandle = 1
    out_r, out_w = win32pipe.CreatePipe(sa, 0)
    err_r, err_w = win32pipe.CreatePipe(sa, 0)
    in_r, in_w = win32pipe.CreatePipe(sa, 0)
    for h in (out_w, err_w, in_r):
        _set_inherit(h, True)
    for h in (out_r, err_r, in_w):
        _set_inherit(h, False)

    job = make_kill_on_close_job()

    si = win32process.STARTUPINFO()
    si.dwFlags = STARTF_USESTDHANDLES
    si.hStdInput = in_r
    si.hStdOutput = out_w
    si.hStdError = err_w

    h_thread = None
    h_proc = None
    try:
        h_proc, h_thread, _pid, _tid = win32process.CreateProcessAsUser(
            token, None, command, None, None, 1,
            CREATE_SUSPENDED, env, cwd, si)
        win32job.AssignProcessToJobObject(job, h_proc)
        win32process.ResumeThread(h_thread)
        h_thread.Close()
        h_thread = None
    except Exception:
        for h in (in_r, in_w, out_r, out_w, err_r, err_w):
            try:
                h.Close()
            except Exception:
                pass
        if h_thread is not None:
            h_thread.Close()
        if h_proc is not None:
            h_proc.Close()
        job.Close()
        raise

    # 父进程侧关闭子进程端（EOF 语义）
    in_r.Close(); out_w.Close(); err_w.Close(); in_w.Close()

    timed_out = False
    start = time.monotonic()
    out_buf, err_buf = [], []
    while True:
        rc = win32event.WaitForSingleObject(h_proc, 50)
        for read_handle, buf in ((out_r, out_buf), (err_r, err_buf)):
            try:
                while True:
                    _, total, _ = win32pipe.PeekNamedPipe(read_handle, 0)
                    if total == 0:
                        break
                    hr, data = win32file.ReadFile(read_handle, total)
                    if hr != 0 and hr not in (
                            win32con.ERROR_BROKEN_PIPE, win32con.ERROR_NO_DATA):
                        break
                    if data:
                        buf.append(data)
            except pywintypes.error:
                pass
        if rc == win32event.WAIT_OBJECT_0:
            break
        if timeout_s and time.monotonic() - start > timeout_s:
            timed_out = True
            win32job.TerminateJobObject(job, 1)
            win32event.WaitForSingleObject(h_proc, 3000)
            break

    exit_code = win32process.GetExitCodeProcess(h_proc)
    out_r.Close(); err_r.Close()
    h_proc.Close()
    job.Close()
    return {
        "exit_code": exit_code,
        "stdout": b"".join(out_buf).decode("utf-8", errors="replace"),
        "stderr": b"".join(err_buf).decode("utf-8", errors="replace"),
        "timed_out": timed_out,
    }
