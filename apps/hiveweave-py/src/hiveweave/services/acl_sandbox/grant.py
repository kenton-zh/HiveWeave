"""WriteGrant —— ACL 写授予原语（spec §4.4/§4.9/§4.12 + §5.2）。

Windows only：pywin32 在非 Windows 平台不可用，此时模块仍可导入
（供跨平台单测 mock），但所有方法抛 SandboxUnavailableError（fail-closed）。

掩码（§4.4 v4 定案，winnt.h 原文）：
- GRANT_MASK = 0x110156 —— 授予写/删，**排除 WRITE_DAC/WRITE_OWNER**
  （授予它们会让受限进程改写工作区内文件对象的 DACL，配合硬链接边界可
  给外部别名加 Everyone 授权越界 —— M7 双靶钉死）。
- CACHE_MASK —— 缓存区去 DELETE/FILE_DELETE_CHILD（断 reparse 删链 §4.10）。
"""

from __future__ import annotations

import os

try:  # pragma: no cover - branch 由平台决定
    import pywintypes
    import win32api
    import win32con
    import win32security
except ImportError:  # non-Windows
    pywintypes = None
    win32api = None
    win32con = None
    win32security = None

from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError

# ── 掩码常量（对齐 DSH win32-abi.ts；注释按 spec §4.4 v4 定案） ─────────
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
WRITE_OWNER = 0x00080000
# winnt.h 原文：STANDARD_RIGHTS_WRITE = READ_CONTROL（0x20000）。
# 注意：0xE0000 是 STANDARD_RIGHTS_REQUIRED 的并集 —— 因 FILE_GENERIC_WRITE
# 只含 READ_CONTROL 一个标准位，两种算法的 GRANT_MASK 数值巧合相同（0x110156），
# 但按 spec §4.4 保持 READ_CONTROL 原义。
STANDARD_RIGHTS_WRITE = READ_CONTROL  # 0x00020000
DELETE = 0x00010000
FILE_DELETE_CHILD = 0x00000040
FILE_GENERIC_WRITE = READ_CONTROL | 0x2 | 0x4 | 0x10 | 0x100 | 0x100000
GRANT_MASK = (FILE_GENERIC_WRITE | DELETE | FILE_DELETE_CHILD) & ~STANDARD_RIGHTS_WRITE  # 0x110156
CACHE_MASK = (FILE_GENERIC_WRITE & ~DELETE & ~FILE_DELETE_CHILD) & ~STANDARD_RIGHTS_WRITE
FILE_ALL_ACCESS = 0x1F01FF  # 仅令牌默认 DACL 注入使用（§4.5）

# OWNER_RIGHTS-only 目录检测（§4.12）—— 该 SID 出现即"无真实主体 ACE"信号
_OWNER_RIGHTS_SID = "S-1-3-4"
# LUA_TOKEN 下 Administrators 是 deny-only —— 只授 Admins 写位的目录对受限
# 令牌不可用，探测时必须排除（§4.12 探测防误通过）
_ADMINISTRATORS_SID = "S-1-5-32-544"
# §P1-1 共享缓存：Authenticated Users（AU）组。受限（LUA）令牌不过滤该启用组，
# 给工作区内缓存类目录补授 AU 写即让**同一工作区的所有受限代理**可共享写入，
# 供测试运行器（.pytest_cache/__pycache__/node_modules/.cache）多 agent 并发复用。
_AUTHENTICATED_USERS_SID = "S-1-5-11"
# Everyone：用于「任何主体都不许删该目录的直接子项」的锁死 ACE
_EVERYONE_SID = "S-1-1-0"


def _require_win32() -> None:
    if win32security is None:
        raise SandboxUnavailableError(
            "ACL sandbox requires Windows (pywin32 unavailable) on this platform",
            platform_side=True,
        )


def _sid_str(sid) -> str:
    return win32security.ConvertSidToStringSid(sid)


def _iter_aces(dacl) -> list[tuple[int, int, int, str]]:
    """PyACL → [(type, flags, mask_unsigned, sid_str)]。mask 可能负数须 & 0xFFFFFFFF。"""
    out = []
    for i in range(dacl.GetAceCount()):
        entry = dacl.GetAce(i)
        # 普通 ACE 3 元组；object ACE（OLE 对象专属）是 5 元组 —— 只取前 3
        (ace_type, ace_flags), mask, s = entry[0], entry[1], entry[2]
        out.append((ace_type, ace_flags, mask & 0xFFFFFFFF, _sid_str(s)))
    return out


def _trustee(sid) -> dict:
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


class WriteGrant:
    """ACL 原语集合。全部为同步阻塞调用 —— 由 service 层经 asyncio.to_thread 执行。"""

    @staticmethod
    def ace_present(path: str, sid: str, mask: int = GRANT_MASK) -> bool:
        """verify-then-skip 探针：目标上是否已有「完全相同」的授予 ACE。

        与 grant_standing 的跳过判据一致：目录须带 OI/CI 继承位，
        掩码须逐位相等 —— 防"有 ACE 但掩码不对"的假跳过。
        """
        _require_win32()
        try:
            sd = win32security.GetNamedSecurityInfo(
                path, win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION)
        except pywintypes.error:
            return False  # 路径不存在 —— 视为未授予（调用方决定是否创建）
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:
            return False
        flags = OI_CI if os.path.isdir(path) else 0
        for ace_type, ace_flags, m, s in _iter_aces(dacl):
            if (s == sid and m == mask
                    and (flags == 0 or ace_flags & OI_CI == OI_CI)
                    and ace_type == ACE_ALLOWED):
                return True
        return False

    @staticmethod
    def grant_standing(path: str, sid: str, mask: int = GRANT_MASK) -> bool:
        """授予能力 SID 写 ACE（目录 OI/CI 继承），精确 ACE 跳过。

        返回 True=实际写盘（SetNamedSecurityInfo 会急切把可继承 ACE 重新
        传播整棵树，大树上数十秒 —— 精确跳过保证每树每机只传播一次）。
        """
        _require_win32()
        if WriteGrant.ace_present(path, sid, mask):
            return False
        flags = OI_CI if os.path.isdir(path) else 0
        dacl = WriteGrant._read_dacl(path)
        if dacl is None:
            raise SandboxUnavailableError(
                f"grant target has no DACL (NULL DACL): {path}",
                api_name="GetSecurityDescriptorDacl")
        sid_obj = win32security.ConvertStringSidToSid(sid)
        dacl.SetEntriesInAcl([_explicit_access(
            sid_obj, mask, win32security.GRANT_ACCESS, flags)])
        WriteGrant._write_dacl(path, dacl)
        return True

    @staticmethod
    def revoke_revocable(path: str, sid: str) -> None:
        """撤销 temp 类 revocable ACE。"""
        _require_win32()
        if not os.path.exists(path):
            return
        dacl = WriteGrant._read_dacl(path)
        if dacl is None:
            return
        sid_obj = win32security.ConvertStringSidToSid(sid)
        dacl.SetEntriesInAcl([_explicit_access(
            sid_obj, 0, win32security.REVOKE_ACCESS, 0)])
        WriteGrant._write_dacl(path, dacl)

    @staticmethod
    def grant_revocable(path: str, sid: str, mask: int = GRANT_MASK,
                        ledger: list | None = None) -> None:
        """§5.2/§4.4：revocable 授予 —— **先记录后授予**，中途失败回滚已铺项。

        ledger 由调用方持有（记录 revocable 项以便 dismiss/项目删除/后端退出
        撤销）；任一环节抛异常时按记录逆序 revoke（standing 不撤 —— 预期终态）。
        """
        if ledger is not None:
            ledger.append((path, sid, mask))
        try:
            WriteGrant.grant_standing(path, sid, mask)
        except Exception:
            if ledger is not None:
                for p, s, _m in reversed(ledger):
                    try:
                        WriteGrant.revoke_revocable(p, s)
                    except Exception:
                        pass
            raise

    @staticmethod
    def grant_shared_cache_write(path: str, mask: int = CACHE_MASK) -> bool:
        """§P1-1 工作区内共享缓存目录补授 AU 写（多 agent 共享测试缓存）。

        受限进程用 Path.mkdir / pytest 创建的缓存目录（.pytest_cache /
        __pycache__ / node_modules/.cache）常是 OWNER_RIGHTS-only 且因令牌
        DefaultDacl 不继承父目录可继承 ACE —— 同一缓存目录（如 MAIN 下
        .pytest_cache，bash_main 共享项目根）被多个 worktree 的受限令牌
        互相写/删时 EPERM（M1 沙箱临时目录墙的共享侧）。对工作区内存现的
        缓存类目录补授 Authenticated Users 写（OI/CI 继承）：受限令牌保留
        AU 组，写进该目录一律放行；`mask` 默认 CACHE_MASK（去 DELETE /
        FILE_DELETE_CHILD，禁单代理删共享产物）。verify-then-skip，幂等。
        """
        _require_win32()
        if WriteGrant.ace_present(path, _AUTHENTICATED_USERS_SID, mask):
            return False
        flags = OI_CI if os.path.isdir(path) else 0
        dacl = WriteGrant._read_dacl(path)
        if dacl is None:
            raise SandboxUnavailableError(
                f"shared-cache grant target has no DACL (NULL DACL): {path}",
                api_name="GetSecurityDescriptorDacl")
        sid_obj = win32security.ConvertStringSidToSid(_AUTHENTICATED_USERS_SID)
        dacl.SetEntriesInAcl([_explicit_access(
            sid_obj, mask, win32security.GRANT_ACCESS, flags)])
        WriteGrant._write_dacl(path, dacl)
        return True

    @staticmethod
    def break_inheritance(path: str) -> None:
        """PROTECTED DACL（§4.9）：复制现有 ACE 为显式 + 阻断父继承。

        幂等：已 PROTECTED 则跳过。用于 `.hiveweave` 子树 —— 项目根的可
        继承 ACE 传播到 `.hiveweave` 即止，data.db/平台系统区对一切受限
        令牌 pass-2 落空。NULL DACL 拒绝处理（不默默转空 ACL）。
        """
        _require_win32()
        if not os.path.isdir(path):
            return
        if WriteGrant._is_dacl_protected(path):
            return
        dacl = WriteGrant._read_dacl(path)
        if dacl is None:
            raise SandboxUnavailableError(
                f"break_inheritance target has NULL DACL: {path} "
                f"(refuse to silently convert to empty ACL)",
                api_name="GetSecurityDescriptorDacl")
        win32security.SetNamedSecurityInfo(
            path, win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            WriteGrant._owner(path), WriteGrant._group(path), dacl, None)

    # ── #2 GitSpawn 治本：git 引导文件的「封条」原语（2026-09-15） ──────
    @staticmethod
    def list_aces(path: str) -> list[tuple[str, int, int]]:
        """[(sid_str, ace_flags, mask)] —— 公开只读视图（不存在的路径 ⇒ []）。"""
        _require_win32()
        if not os.path.exists(path):
            return []
        try:
            dacl = WriteGrant._read_dacl(path)
        except (pywintypes.error, OSError):
            return []
        if dacl is None:
            return []
        return _iter_aces(dacl)

    @staticmethod
    def seal_agent_aces(path: str, sid_strs: set[str], *,
                        strip_platform_delete: bool = False) -> bool:
        """封条：把给定受限 SID 的 ACE 从 *path* 摘除，其余 ACE 转显式 + PROTECTED。

        返回 True = 实际写盘；**幂等**（已摘除且已 PROTECTED ⇒ False，不再写盘
        —— 与 grant_standing 的精确跳过同一动机：SetNamedSecurityInfo 会急切
        重新传播，大树上很贵）。

        为什么必须同时做三件事（缺任何一件都是假封条）：
        1. **摘除**而不是加 DENY：受限令牌 pass-2 落空的判据是「没有任何 ACE 授予
           该受限 SID」—— 与 `_ensure_standing_grants` 的 verify-then-skip 同一
           机制，不引入第二种判定语义。
        2. **其余 ACE 转显式**（清 INHERITED_ACE 位）：PROTECTED 之后 Windows 不再
           从父目录算继承；若「用户/AuthUsers 的写」原本是继承来的，直接置
           PROTECTED 会把它们一起丢掉 ⇒ **平台自己的 `git config <写>` 会失败**。
           （计划 §四 担心的「收权限会坏三处」是**只读文件属性**探针的产物，
           不是 ACL 方案的 —— 见 `scripts/probe_git_write_surface.py` F1。）
        3. **PROTECTED**：否则下一轮 standing grant 的父目录 OI/CI 传播会把封条
           重新灌开 —— 与 `.hiveweave` 的 `break_inheritance` 同族不变量。

        ⚠ 只接受可由 `AddAccessAllowedAceEx`/`AddAccessDeniedAceEx` 重建的 ACE
        （allow/deny）；出现其它类型（object ACE 等）**拒改**并抛错 —— 重建时
        丢掉一个不认识的 ACE 等于静默削平台权限，宁可 fail-closed。
        """
        _require_win32()
        if not os.path.exists(path):
            return False
        sd = win32security.GetNamedSecurityInfo(
            path, win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:
            raise SandboxUnavailableError(
                f"seal target has NULL DACL (refuse to treat as sealed): {path}",
                api_name="GetSecurityDescriptorDacl")
        control, _rev = sd.GetSecurityDescriptorControl()
        protected = bool(control & win32security.SE_DACL_PROTECTED)
        # ⚠ 只算 **ALLOW** ACE：本模块自己会往这些对象上加 DENY（deny_delete_child），
        # 若把 deny 也算成「还有 agent ACE」，则每次都判定为未达标 ⇒ **每轮 spawn
        # 全量重建 DACL**（审计 B1 实测），封条承诺的稳态零写盘就没了。
        has_agent_ace = any(
            ace_type == ACE_ALLOWED and s in sid_strs
            for ace_type, _f, _m, s in _iter_aces(dacl))
        # ⚠ strip 档下**不能**只看能力 SID 就短路：`unlock_for_delete` 解锁时补的是
        #   **平台主体**的 DELETE/DC（判定看不见）⇒ 只判 has_agent_ace 会把「已解锁」
        #   误判成「已封」，**一次解锁永久生效**（实测：解锁后再跑封条，
        #   平台 DELETE 仍在 ⇒ 删得掉）。故 strip 档必须确认「没有任何 ALLOW ACE
        #   还带 DELETE/FC」，否则照常重建。
        strip_leftover = any(
            ace_type == ACE_ALLOWED and mask & (DELETE | FILE_DELETE_CHILD)
            for ace_type, _f, mask, _s in _iter_aces(dacl))
        if protected and not has_agent_ace and not (
                strip_platform_delete and strip_leftover):
            return False
        new_acl = win32security.ACL()
        for ace_type, ace_flags, mask, sid in _iter_aces(dacl):
            if ace_type == ACE_ALLOWED and sid in sid_strs:
                continue
            if strip_platform_delete and ace_type == ACE_ALLOWED:
                # 「双阶段」的锁死期：连**平台主体**的 DELETE/DC 一起去掉 ⇒ 谁都替换
                # 不掉这个文件（Windows 删子项两条准入路径都被堵）。
                # ⚠ 代价：平台自己也**不再能** lock+rename 重写它（`git config <写>`
                # 会失败）⇒ 只对「平台在锁死之后确实不需要再写」的载体启用
                # （`.git/config` / `config.worktree`；见 acl_sandbox/service.py 的调用点）。
                mask = mask & ~(DELETE | FILE_DELETE_CHILD)
            flags = ace_flags & ~_INHERITED_ACE
            # 掩码符号：GetAce 给的是无符号值，AddAccess* 收 C long ⇒ 高位掩码
            # 会 OverflowError（审计 B2）。归一到有符号 32 位。
            signed = mask - 0x100000000 if mask > 0x7FFFFFFF else mask
            if ace_type == ACE_ALLOWED:
                new_acl.AddAccessAllowedAceEx(
                    win32security.ACL_REVISION, flags, signed,
                    win32security.ConvertStringSidToSid(sid))
            elif ace_type == ACE_DENIED:
                new_acl.AddAccessDeniedAceEx(
                    win32security.ACL_REVISION, flags, signed,
                    win32security.ConvertStringSidToSid(sid))
            else:
                raise SandboxUnavailableError(
                    f"seal target has ACE type {ace_type} that cannot be "
                    f"rebuilt (would silently drop platform rights): {path}")
        win32security.SetNamedSecurityInfo(
            path, win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            WriteGrant._owner(path), WriteGrant._group(path), new_acl, None)
        return True

    @staticmethod
    def deny_delete_child(path: str, sid_strs: set[str]) -> int:
        """对 *path* 加 DENY ACE（`DELETE|FILE_DELETE_CHILD`，本对象、不继承）。

        ⚠ **这不是「防删」的保证，只是部分防线**（审计 2026-09-15 B1 实测）：
        Windows 删子项有两条准入路径 —— 子对象自己的 DELETE **或** 父目录的
        FILE_DELETE_CHILD；而本仓的能力 ACE 语义是 **pass-2**，可**删**这一侧
        走的是 **pass-1**（普通令牌的 user ACE，它对平台自有文件有 FILE_ALL_ACCESS）
        ⇒ 受限 agent 照样删得掉封条文件（实测 D1–D3 修前修后同为 DELETED）。
        本函数堵的是 DC 那条路（挡住「子对象自己没有 DELETE」时的删除），成本为零，
        故保留 —— 但**不得**在别处声称「文件已经不能被删」。
        真正的防删要么收 user 侧的 DELETE（= 平台自己也不能 lock+rename 改它，
        见 fixqueue #2「双阶段」），要么在平台侧改用显式 `--git-dir` 之类的
        **信任锚**，不依赖「文件还在不在」。

        返回实际新增的 ACE 数（幂等：已有同 SID 的等价 deny ⇒ 跳过）。
        """
        _require_win32()
        if not os.path.isdir(path):
            return 0
        dacl = WriteGrant._read_dacl(path)
        if dacl is None:
            raise SandboxUnavailableError(
                f"deny_delete_child target has NULL DACL: {path}",
                api_name="GetSecurityDescriptorDacl")
        covered = {
            sid for ace_type, _f, mask, sid in _iter_aces(dacl)
            if ace_type == ACE_DENIED and mask & SEAL_DENY_MASK == SEAL_DENY_MASK
        }
        todo = sorted(sid_strs - covered)
        if not todo:
            return 0
        dacl.SetEntriesInAcl([
            _explicit_access(
                win32security.ConvertStringSidToSid(sid),
                SEAL_DENY_MASK, win32security.DENY_ACCESS, 0)
            for sid in todo
        ])
        WriteGrant._write_dacl(path, dacl)
        return len(todo)

    @staticmethod
    def unlock_for_delete(path: str) -> bool:
        """把 *path* 恢复成「可删」：给当前主体补 DELETE + 摘掉父目录的 Everyone-FC 锁死 ACE。

        为什么需要：**锁死档**（`seal_agent_aces(strip_platform_delete=True)`）+
        **全主体禁删子项**（`deny_child_delete_for_all`）是给 agent 看的锁，但平台
        **自己的清理路径也要动这些路径**（`git worktree remove` 要删整个 `<gitdir>`；
        删项目/清残留的 `rmtree` 会走到 `.git` 附近）⇒ 清理前解锁，否则会
        PermissionError（或被 `rmtree` 的 onerror 吞成 debug 日志）。
        实现上用**属主改 DACL**（owner 天生有 WRITE_DAC）—— 所以这不需要额外特权，
        也说明「锁」防的是 agent 而不是管理员。

        返回 True = 做过改动。
        """
        _require_win32()
        changed = False
        parent = os.path.dirname(os.path.abspath(path))
        # ① 父目录：摘掉 Everyone 的 FC deny（只摘这一条，别的 deny 不动）
        if os.path.isdir(parent):
            dacl = WriteGrant._read_dacl(parent)
            if dacl is not None:
                new_acl = win32security.ACL()
                removed = False
                for ace_type, ace_flags, mask, sid in _iter_aces(dacl):
                    if (ace_type == ACE_DENIED and sid == _EVERYONE_SID
                            and mask & FILE_DELETE_CHILD):
                        removed = True
                        continue
                    flags = ace_flags & ~_INHERITED_ACE
                    signed = mask - 0x100000000 if mask > 0x7FFFFFFF else mask
                    if ace_type == ACE_ALLOWED:
                        new_acl.AddAccessAllowedAceEx(
                            win32security.ACL_REVISION, flags, signed,
                            win32security.ConvertStringSidToSid(sid))
                    elif ace_type == ACE_DENIED:
                        new_acl.AddAccessDeniedAceEx(
                            win32security.ACL_REVISION, flags, signed,
                            win32security.ConvertStringSidToSid(sid))
                if removed:
                    win32security.SetNamedSecurityInfo(
                        parent, win32security.SE_FILE_OBJECT,
                        win32security.DACL_SECURITY_INFORMATION
                        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                        WriteGrant._owner(parent), WriteGrant._group(parent),
                        new_acl, None)
                    changed = True
        # ② 目标本身：给当前主体（用户）补 DELETE
        if os.path.exists(path):
            dacl = WriteGrant._read_dacl(path)
            if dacl is not None:
                user_sid = WriteGrant._current_subject_sids()
                has_delete = any(
                    ace_type == ACE_ALLOWED and s in user_sid
                    and mask & DELETE
                    for ace_type, _f, mask, s in _iter_aces(dacl))
                if not has_delete:
                    sid = win32security.OpenProcessToken(
                        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
                    try:
                        user = win32security.GetTokenInformation(
                            sid, win32security.TokenUser)[0]
                    finally:
                        sid.Close()
                    dacl.SetEntriesInAcl([_explicit_access(
                        user, DELETE | FILE_DELETE_CHILD,
                        win32security.GRANT_ACCESS, 0)])
                    WriteGrant._write_dacl(path, dacl)
                    changed = True
        return changed

    @staticmethod
    def deny_child_delete_for_all(path: str) -> int:
        """对 *path* 加「**任何主体**都不许删它的**直接子项**」的 DENY（mask=FILE_DELETE_CHILD）。

        为什么必要（09-15 实测的「两条准入路径」再现）：Windows 删子项可走
        ① 子对象自己的 DELETE，**或** ② 父目录的 FILE_DELETE_CHILD —— 而 ① 走 pass-1
        （普通令牌的 user ACE，对平台自有文件有 FILE_ALL_DELETE），② 也走 pass-1。
        所以只摘「能力 SID 的 ACE」根本挡不住删（D1–D3 实测 DELETED）；「锁死档」把
        子对象的 DELETE 摘掉之后，只剩 ② 这条路 ⇒ 必须 deny **父目录**的 DC，
        而且要 deny 给**所有主体**（deny 给某几个 SID 挡不住 user 那条）。

        ⚠ 为什么 deny 的是 FC 而不是 DELETE：DELETE 在**目录自己**身上，deny 它会把
        「删这个目录」也堵掉（而平台要保留删该目录的能力，例如 `git worktree prune`）。
        FC 只管「删它的子项」这一条路；子项**自己**有 DELETE 的（正常继承来的）
        照旧可删 —— 只有被锁死的那些（已摘 DELETE）才真的删不掉。

        返回实际新增的 ACE 数（幂等）。
        """
        _require_win32()
        if not os.path.isdir(path):
            return 0
        dacl = WriteGrant._read_dacl(path)
        if dacl is None:
            raise SandboxUnavailableError(
                f"deny_child_delete target has NULL DACL: {path}",
                api_name="GetSecurityDescriptorDacl")
        for ace_type, _f, mask, sid in _iter_aces(dacl):
            if (ace_type == ACE_DENIED and sid == _EVERYONE_SID
                    and mask & FILE_DELETE_CHILD):
                return 0
        dacl.SetEntriesInAcl([_explicit_access(
            win32security.ConvertStringSidToSid(_EVERYONE_SID),
            FILE_DELETE_CHILD, win32security.DENY_ACCESS, 0)])
        WriteGrant._write_dacl(path, dacl)
        return 1

    @staticmethod
    def has_subject_write_ace(path: str) -> bool:
        """§4.12 部署前提探测：DACL 是否授予「当前令牌身份」写权。

        Python tempfile/Path.mkdir 产物是 OWNER_RIGHTS-only（SYSTEM/Admins/
        OWNER_RIGHTS）—— 对 write-restricted 令牌不可用：UAC filtered 令牌
        下 Admins 是 deny-only、SYSTEM/OWNER_RIGHTS 与用户无关，用户对该目录
        无任何访问权。探测方法 = 取当前令牌的用户 SID + 启用组 SID 集，扫
        DACL 看是否有写位 ACE 授予其中任一主体（含继承自父目录的 AuthUsers/
        User ACE —— 用户常规目录正是靠继承满足）。
        """
        _require_win32()
        subject_sids = WriteGrant._current_subject_sids()
        try:
            sd = win32security.GetNamedSecurityInfo(
                path, win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION)
        except pywintypes.error:
            return False
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:
            return False
        write_bits = FILE_GENERIC_WRITE | DELETE | FILE_DELETE_CHILD | WRITE_DAC | WRITE_OWNER
        for ace_type, _ace_flags, m, s in _iter_aces(dacl):
            if ace_type == ACE_ALLOWED and (m & write_bits) and s in subject_sids:
                return True
        return False

    @staticmethod
    def _current_subject_sids() -> set[str]:
        """当前令牌的用户 SID + 启用组 SID（排除 deny-only 与提权专属组）。

        排除 Administrators（S-1-5-32-544）：LUA_TOKEN 下它是 deny-only，
        只授 Admins 写位的目录对受限令牌不可用 —— 探测含它会误通过 §4.12。
        """
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        try:
            user, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
            sids = {_sid_str(user)}
            groups = win32security.GetTokenInformation(token, win32security.TokenGroups)
            for sid, attr in groups:
                if (attr & win32security.SE_GROUP_ENABLED
                        and not (attr & win32security.SE_GROUP_USE_FOR_DENY_ONLY)):
                    s = _sid_str(sid)
                    if s == _ADMINISTRATORS_SID:
                        continue
                    sids.add(s)
            return sids
        finally:
            token.Close()

    # ── 内部工具 ─────────────────────────────────────────────
    @staticmethod
    def _read_dacl(path: str):
        sd = win32security.GetNamedSecurityInfo(
            path, win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION)
        return sd.GetSecurityDescriptorDacl()

    @staticmethod
    def _write_dacl(path: str, dacl) -> None:
        win32security.SetNamedSecurityInfo(
            path, win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
            WriteGrant._owner(path), WriteGrant._group(path), dacl, None)

    @staticmethod
    def _owner(path: str):
        try:
            return win32security.GetNamedSecurityInfo(
                path, win32security.SE_FILE_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION
            ).GetSecurityDescriptorOwner()
        except pywintypes.error:
            return None

    @staticmethod
    def _group(path: str):
        try:
            return win32security.GetNamedSecurityInfo(
                path, win32security.SE_FILE_OBJECT,
                win32security.GROUP_SECURITY_INFORMATION
            ).GetSecurityDescriptorGroup()
        except pywintypes.error:
            return None

    @staticmethod
    def _is_dacl_protected(path: str) -> bool:
        try:
            sd = win32security.GetNamedSecurityInfo(
                path, win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION)
        except pywintypes.error:
            return False
        control, _rev = sd.GetSecurityDescriptorControl()
        return bool(control & win32security.SE_DACL_PROTECTED)


# ── 继承位 / ACE 类型（win32con 在非 Windows 下为 None） ───────────────
OI_CI = (win32con.CONTAINER_INHERIT_ACE | win32con.OBJECT_INHERIT_ACE
         if win32con is not None else 0)
ACE_ALLOWED = (win32con.ACCESS_ALLOWED_ACE_TYPE if win32con is not None else 0)
ACE_DENIED = (win32con.ACCESS_DENIED_ACE_TYPE if win32con is not None else 0)
# INHERITED_ACE：ACE 上的「我是继承来的」标记（win32security.INHERITED_ACE=0x10）。
# 封条重建时把它清掉 —— 见 seal_agent_aces 第 2 条理由。
_INHERITED_ACE = getattr(win32security, "INHERITED_ACE", 0x10)
# 父目录禁「删子项」用的掩码：DELETCHILD 的两条准入路径都要堵
SEAL_DENY_MASK = DELETE | FILE_DELETE_CHILD  # 0x10040
