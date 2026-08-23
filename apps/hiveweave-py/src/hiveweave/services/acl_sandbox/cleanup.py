"""ACL 沙箱迁移清理工具（spec §7.4 P2）—— 移除遗留的死能力 SID ACE。

workspace 迁移/路径改名 → 新路径派生新 SID 全套；旧树遗留的 `S-1-4-*`
能力 ACE 惰性无害（新令牌不再携带旧 SID，旧 ACE 无消费方），但会累积。
本工具遍历目录树，删除所有 `S-1-4-*` 前缀 ACE（走本模块代码路径 ——
实测 `icacls /remove` 对该形态失败，须经 win32security 原语）。

用法：
    python -m hiveweave.services.acl_sandbox.cleanup <path> [--dry-run]

仅 Windows 生效；非 Windows 直接报错退出。只处理 `S-1-4-*`（本沙箱能力
SID 专属区间，见 spec §4.3），不触碰用户其他 ACE。
"""

from __future__ import annotations

import argparse
import os
import sys

try:  # pragma: no cover - branch 由平台决定
    import pywintypes
    import win32security
except ImportError:  # non-Windows
    pywintypes = None
    win32security = None

from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError

# 本沙箱能力 SID 全部落在 S-1-4-（受限令牌专属区间，spec §4.3）。
_CAPABILITY_SID_PREFIX = "S-1-4-"


def _require_win32() -> None:
    if win32security is None:
        raise SandboxUnavailableError(
            "ACL sandbox cleanup requires Windows (pywin32 unavailable)")


def _collect_capability_sids(path: str) -> list[str]:
    """读取路径 DACL 中全部 `S-1-4-*` 能力 SID（去重）。"""
    _require_win32()
    try:
        sd = win32security.GetNamedSecurityInfo(
            path, win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION)
    except pywintypes.error:
        return []
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        return []
    sids: set[str] = set()
    for i in range(dacl.GetAceCount()):
        entry = dacl.GetAce(i)
        try:
            s = win32security.ConvertSidToStringSid(entry[2])
        except (pywintypes.error, IndexError, AttributeError):
            continue
        if s.startswith(_CAPABILITY_SID_PREFIX):
            sids.add(s)
    return sorted(sids)


def _remove_sids(path: str, sids: list[str]) -> None:
    """经 SetEntriesInAcl(REVOKE) 移除指定 SID 的全部 ACE。"""
    _require_win32()
    if not sids:
        return
    sd = win32security.GetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        return
    entries = []
    for s in sids:
        try:
            sid_obj = win32security.ConvertStringSidToSid(s)
        except pywintypes.error:
            continue
        entries.append({
            "AccessPermissions": 0,
            "AccessMode": win32security.REVOKE_ACCESS,
            "Inheritance": 0,
            "Trustee": {
                "MultipleTrustee": None,
                "MultipleTrusteeOperation": 0,
                "TrusteeForm": win32security.TRUSTEE_IS_SID,
                "TrusteeType": win32security.TRUSTEE_IS_UNKNOWN,
                "Identifier": sid_obj,
            },
        })
    if not entries:
        return
    dacl.SetEntriesInAcl(entries)
    win32security.SetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        win32security.GetNamedSecurityInfo(
            path, win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION
        ).GetSecurityDescriptorOwner(),
        win32security.GetNamedSecurityInfo(
            path, win32security.SE_FILE_OBJECT,
            win32security.GROUP_SECURITY_INFORMATION
        ).GetSecurityDescriptorGroup(),
        dacl, None)


def clean_tree(root: str, dry_run: bool = False) -> dict:
    """遍历 root 全树，移除 `S-1-4-*` 能力 SID ACE。

    Returns: {"scanned": int, "cleaned": int, "errors": list[str]}
    """
    _require_win32()
    if not os.path.exists(root):
        raise SandboxUnavailableError(f"清理目标不存在: {root}")
    scanned = 0
    cleaned = 0
    errors: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames + [""]:  # "" = 目录自身
            p = dirpath if not name else os.path.join(dirpath, name)
            scanned += 1
            try:
                sids = _collect_capability_sids(p)
                if sids:
                    cleaned += len(sids)
                    if not dry_run:
                        _remove_sids(p, sids)
            except Exception as e:  # noqa: BLE001 —— 单文件失败不中断遍历
                errors.append(f"{p}: {e}")
    return {"scanned": scanned, "cleaned": cleaned, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hiveweave-sandbox-clean",
        description="移除 workspace 迁移后遗留的死能力 SID ACE（spec §7.4）")
    parser.add_argument("path", help="要清理的目录（项目根 / worktree / 外部目录）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报告不写盘")
    args = parser.parse_args(argv)

    if not sys.platform.startswith("win"):
        print("仅支持 Windows（ACL 沙箱）", file=sys.stderr)
        return 2
    try:
        result = clean_tree(args.path, dry_run=args.dry_run)
    except SandboxUnavailableError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    print(
        f"scanned={result['scanned']} cleaned_aces={result['cleaned']} "
        f"errors={len(result['errors'])} dry_run={args.dry_run}")
    for e in result["errors"][:20]:
        print(f"  err: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
