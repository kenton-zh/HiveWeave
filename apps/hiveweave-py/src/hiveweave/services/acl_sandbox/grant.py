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


def _require_win32() -> None:
    if win32security is None:
        raise SandboxUnavailableError(
            "ACL sandbox requires Windows (pywin32 unavailable) on this platform"
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
