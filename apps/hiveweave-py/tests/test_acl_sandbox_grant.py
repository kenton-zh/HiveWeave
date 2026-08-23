"""WriteGrant 单测（spec §4.4/§4.9/§4.12 + §12.1）。mock win32，跨平台。

钉：
- 精确 ACE 跳过（SetNamedSecurityInfo 计数：首铺 1 次、复跑 0 次 —— M3 靶）
- PROTECTED 裁剪幂等（§4.9）
- 主体 ACE 探测（§4.12：OWNER_RIGHTS-only → False）
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

import hiveweave.services.acl_sandbox.grant as gmod
from hiveweave.services.acl_sandbox.grant import (
    CACHE_MASK,
    GRANT_MASK,
    WriteGrant,
)
from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError

ACCESS_ALLOWED_ACE_TYPE = 0
OI_CI = 3  # CONTAINER_INHERIT_ACE(2) | OBJECT_INHERIT_ACE(1)
GRANT_ACCESS = 1
REVOKE_ACCESS = 2
OWNER_RIGHTS_SID = "S-1-3-4"


class FakeAce:
    def __init__(self, ace_type, flags, mask, sid):
        self.ace_type = ace_type
        self.flags = flags
        self.mask = mask
        self.sid = sid


class FakeDacl:
    def __init__(self, aces=None):
        self.aces = aces if aces is not None else []

    def GetAceCount(self) -> int:
        return len(self.aces)

    def GetAce(self, i):
        a = self.aces[i]
        return ((a.ace_type, a.flags), a.mask, a.sid)

    def SetEntriesInAcl(self, entries) -> None:  # 原地修改，返回 None（pywin32 形态）
        for e in entries:
            if e["AccessMode"] == GRANT_ACCESS:
                self.aces.append(FakeAce(
                    ACCESS_ALLOWED_ACE_TYPE, e["Inheritance"],
                    e["AccessPermissions"], e["Trustee"]["Identifier"]))
            elif e["AccessMode"] == REVOKE_ACCESS:
                target = e["Trustee"]["Identifier"]
                self.aces = [a for a in self.aces if a.sid != target]


class FakeSd:
    def __init__(self, dacl, protected=False):
        self._dacl = dacl
        self._control = 0x1000 if protected else 0

    def GetSecurityDescriptorDacl(self):
        return self._dacl

    def GetSecurityDescriptorOwner(self):
        return "OWNER"

    def GetSecurityDescriptorGroup(self):
        return "GROUP"

    def GetSecurityDescriptorControl(self):
        return self._control, 1


def _fake_ws(dacl, sd=None, protected=False) -> MagicMock:
    fake = MagicMock()
    fake.GetNamedSecurityInfo.return_value = sd or FakeSd(dacl, protected=protected)
    fake.SetNamedSecurityInfo = MagicMock()
    fake.ConvertStringSidToSid = lambda s: s
    fake.ConvertSidToStringSid = lambda s: s
    fake.GRANT_ACCESS = GRANT_ACCESS
    fake.REVOKE_ACCESS = REVOKE_ACCESS
    fake.SE_FILE_OBJECT = 1
    fake.DACL_SECURITY_INFORMATION = 4
    fake.OWNER_SECURITY_INFORMATION = 1
    fake.GROUP_SECURITY_INFORMATION = 2
    fake.PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    fake.SE_DACL_PROTECTED = 0x1000
    fake.TRUSTEE_IS_SID = 1
    fake.TRUSTEE_IS_UNKNOWN = 0
    fake.TokenUser = 1
    fake.TokenGroups = 2
    fake.SE_GROUP_ENABLED = 0x4
    fake.SE_GROUP_USE_FOR_DENY_ONLY = 0x10
    fake.TOKEN_QUERY = 0x8
    fake.OpenProcessToken = MagicMock(return_value=MagicMock(name="tok"))
    # 默认身份：用户 + Users 组（均启用）
    fake.GetTokenInformation = MagicMock(side_effect=lambda tok, cls: (
        ("S-1-5-21-123", 0) if cls == fake.TokenUser
        else [("S-1-5-21-123", fake.SE_GROUP_ENABLED),
              ("S-1-5-32-545", fake.SE_GROUP_ENABLED)]))
    return fake


def _patch_win32(dacl, *, isdir=True, exists=True, protected=False):
    fake = _fake_ws(dacl, protected=protected)
    fake_api = MagicMock()
    fake_api.GetCurrentProcess.return_value = "PROC"
    stack = [
        patch.object(gmod, "win32security", fake),
        patch.object(gmod, "win32api", fake_api),
        patch.object(gmod, "win32con", MagicMock()),
        patch.object(gmod, "OI_CI", OI_CI),
        patch.object(gmod, "ACE_ALLOWED", ACCESS_ALLOWED_ACE_TYPE),
        patch.object(os.path, "isdir", return_value=isdir),
        patch.object(os.path, "exists", return_value=exists),
    ]
    for p in stack:
        p.start()
    return fake, stack


def _stop_stack(stack) -> None:
    for p in reversed(stack):
        p.stop()


def test_exact_ace_skip_single_propagation() -> None:
    """精确 ACE 跳过：首铺写 1 次，复跑 0 次（M3 靶）。"""
    dacl = FakeDacl()
    fake, stack = _patch_win32(dacl)
    try:
        g = WriteGrant()
        path, sid = r"D:\ws", "S-1-4-1-1"
        assert g.grant_standing(path, sid, GRANT_MASK) is True
        assert fake.SetNamedSecurityInfo.call_count == 1
        # 复跑：ACE 已存在 → 跳过
        assert g.grant_standing(path, sid, GRANT_MASK) is False
        assert fake.SetNamedSecurityInfo.call_count == 1
        # 写出的 ACE 形态：ALLOWED + OI/CI + GRANT_MASK
        assert len(dacl.aces) == 1
        a = dacl.aces[0]
        assert a.ace_type == ACCESS_ALLOWED_ACE_TYPE
        assert a.flags == OI_CI
        assert a.mask == GRANT_MASK
        assert a.sid == sid
    finally:
        _stop_stack(stack)


def test_ace_present_distinguishes_mask() -> None:
    """verify-then-skip 探针：掩码须逐位相等（防"有 ACE 但掩码不对"假跳过）。"""
    dacl = FakeDacl([FakeAce(ACCESS_ALLOWED_ACE_TYPE, OI_CI, CACHE_MASK, "S-1-4-1-1")])
    fake, stack = _patch_win32(dacl)
    try:
        g = WriteGrant()
        assert g.ace_present(r"D:\ws", "S-1-4-1-1", GRANT_MASK) is False
        assert g.ace_present(r"D:\ws", "S-1-4-1-1", CACHE_MASK) is True
    finally:
        _stop_stack(stack)


def test_ace_present_requires_oi_ci_on_dir() -> None:
    """目录 ACE 必须带 OI/CI 继承位才算在场（继承传播的根基）。"""
    dacl = FakeDacl([FakeAce(ACCESS_ALLOWED_ACE_TYPE, 0, GRANT_MASK, "S-1-4-1-1")])
    fake, stack = _patch_win32(dacl)
    try:
        g = WriteGrant()
        assert g.ace_present(r"D:\ws", "S-1-4-1-1", GRANT_MASK) is False
    finally:
        _stop_stack(stack)


def test_break_inheritance_idempotent() -> None:
    """§4.9 PROTECTED 裁剪幂等：未受保护 → 写 PROTECTED 位；已受保护 → 跳过。"""
    dacl = FakeDacl()
    fake, stack = _patch_win32(dacl, protected=False)
    try:
        g = WriteGrant()
        g.break_inheritance(r"D:\ws\.hiveweave")
        assert fake.SetNamedSecurityInfo.call_count == 1
        call_flags = fake.SetNamedSecurityInfo.call_args[0][2]
        assert call_flags & 0x80000000  # PROTECTED_DACL_SECURITY_INFORMATION
    finally:
        _stop_stack(stack)

    # 已 PROTECTED（第二次）→ 不再写
    fake2, stack2 = _patch_win32(FakeDacl(), protected=True)
    try:
        g = WriteGrant()
        g.break_inheritance(r"D:\ws\.hiveweave")
        assert fake2.SetNamedSecurityInfo.call_count == 0
    finally:
        _stop_stack(stack2)


def test_revoke_removes_ace() -> None:
    """revoke_revocable：REVOKE_ACCESS 移除该 SID 全部 ACE。"""
    dacl = FakeDacl([FakeAce(ACCESS_ALLOWED_ACE_TYPE, OI_CI, GRANT_MASK, "S-1-4-1-1")])
    fake, stack = _patch_win32(dacl)
    try:
        g = WriteGrant()
        g.revoke_revocable(r"D:\ws\t", "S-1-4-1-1")
        assert dacl.aces == []
    finally:
        _stop_stack(stack)


def test_has_subject_write_ace_owner_rights_only_false() -> None:
    """§4.12：OWNER_RIGHTS-only（SYSTEM/Admins/OWNER_RIGHTS）→ 探测失败。"""
    dacl = FakeDacl([
        FakeAce(ACCESS_ALLOWED_ACE_TYPE, 0, GRANT_MASK, "S-1-5-18"),        # SYSTEM
        FakeAce(ACCESS_ALLOWED_ACE_TYPE, 0, GRANT_MASK, "S-1-5-32-544"),   # Administrators
        FakeAce(ACCESS_ALLOWED_ACE_TYPE, 0, GRANT_MASK, OWNER_RIGHTS_SID),  # OWNER_RIGHTS
    ])
    fake, stack = _patch_win32(dacl)
    try:
        assert WriteGrant.has_subject_write_ace(r"D:\ws") is False
    finally:
        _stop_stack(stack)


def test_has_subject_write_ace_with_real_subject_true() -> None:
    """§4.12：含用户/组写 ACE（含继承来的）→ 探测通过。"""
    dacl = FakeDacl([
        FakeAce(ACCESS_ALLOWED_ACE_TYPE, 0, GRANT_MASK, "S-1-5-21-123"),   # 用户
    ])
    fake, stack = _patch_win32(dacl)
    try:
        assert WriteGrant.has_subject_write_ace(r"D:\ws") is True
    finally:
        _stop_stack(stack)


def test_has_subject_write_ace_only_admins_false() -> None:
    """§4.12 防误通过：只授 Administrators 写位 → 探测必须 False。

    LUA_TOKEN 下受限令牌里 Administrators 是 deny-only，只授 Admins 的目录
    对受限令牌不可用 —— 探测含 Admins 会误判可用，运行时才 EACCES。
    """
    dacl = FakeDacl([
        FakeAce(ACCESS_ALLOWED_ACE_TYPE, 0, GRANT_MASK, "S-1-5-32-544"),   # Administrators
    ])
    fake, stack = _patch_win32(dacl)
    try:
        assert WriteGrant.has_subject_write_ace(r"D:\ws") is False
    finally:
        _stop_stack(stack)


def test_grant_revocable_record_before_grant_rollback() -> None:
    """§4.4/§5.2：record-before-grant —— 第二条失败时回滚已铺的第一条。"""
    # 第一次 grant_standing 成功（DACL 空），第二次注入失败
    real = WriteGrant.grant_standing
    calls: list[str] = []
    revokes: list[tuple] = []

    def fake_grant(path, sid, mask=GRANT_MASK):
        calls.append(path)
        if len(calls) == 2:
            raise SandboxUnavailableError("boom", api_name="SetNamedSecurityInfo")
        return real(path, sid, mask)

    dacl = FakeDacl()
    fake, stack = _patch_win32(dacl)
    try:
        with patch.object(WriteGrant, "grant_standing", staticmethod(fake_grant)):
            with patch.object(
                WriteGrant, "revoke_revocable",
                staticmethod(lambda p, s: revokes.append((p, s)))):
                ledger: list = []
                with pytest.raises(SandboxUnavailableError):
                    WriteGrant.grant_revocable(r"D:\ws\t1", "S-1-4-1-1", ledger=ledger)
                    WriteGrant.grant_revocable(r"D:\ws\t2", "S-1-4-1-2", ledger=ledger)
    finally:
        _stop_stack(stack)
    # 回滚：两条（含失败那条）都被 revoke
    assert ("D:\\ws\\t1", "S-1-4-1-1") in revokes
    assert ("D:\\ws\\t2", "S-1-4-1-2") in revokes


def test_grant_on_null_dacl_fails_closed() -> None:
    """NULL DACL（无安全描述符）→ SandboxUnavailableError，不默默转空 ACL。"""
    dacl = None
    fake, stack = _patch_win32(dacl)
    try:
        g = WriteGrant()
        with pytest.raises(SandboxUnavailableError):
            g.grant_standing(r"D:\ws", "S-1-4-1-1", GRANT_MASK)
    finally:
        _stop_stack(stack)
