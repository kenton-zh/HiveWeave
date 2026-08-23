"""RestrictedTokenFactory —— WRITE_RESTRICTED 令牌构造（spec §4.5/§4.6/§5.3）。

fail-closed：任一步失败抛 SandboxUnavailableError。每命令新建 token
（微秒级，无盘 I/O）。
"""

from __future__ import annotations

try:  # pragma: no cover - branch 由平台决定
    import win32api
    import win32security
except ImportError:  # non-Windows
    win32api = None
    win32security = None

from hiveweave.services.acl_sandbox.errors import SandboxUnavailableError
from hiveweave.services.acl_sandbox.grant import FILE_ALL_ACCESS

# pywin32 未导出的 CreateRestrictedToken flag（对照 DSH win32-abi.ts）
DISABLE_MAX_PRIVILEGE = 0x1
LUA_TOKEN = 0x4
WRITE_RESTRICTED = 0x8
TOKEN_FLAGS = DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED

TOKEN_ALL_NEED = (
    win32security.TOKEN_QUERY
    | win32security.TOKEN_DUPLICATE
    | win32security.TOKEN_ADJUST_DEFAULT
    | win32security.TOKEN_ASSIGN_PRIMARY
) if win32security is not None else 0

SE_GROUP_LOGON_ID = 0xC0000000


def open_current_process_token():
    _require()
    return win32security.OpenProcessToken(win32api.GetCurrentProcess(), TOKEN_ALL_NEED)


def find_logon_sid(token):
    """TokenGroups 里找 SE_GROUP_LOGON_ID（pywin32 直返 [(PySID, attr)]）。"""
    groups = win32security.GetTokenInformation(token, win32security.TokenGroups)
    for sid, attr in groups:
        if (attr & SE_GROUP_LOGON_ID) == SE_GROUP_LOGON_ID:
            return sid
    raise SandboxUnavailableError("no logon SID in token groups")


def _require() -> None:
    if win32security is None:
        raise SandboxUnavailableError(
            "ACL sandbox requires Windows (pywin32 unavailable) on this platform"
        )


def _create_restricted_token(
    write_sids: list[str],
    temp_sid: str,
    *,
    include_everyone: bool = True,
    inject_default_dacl: bool = True,
):
    """低层受限令牌构造（§5.3/§4.5/§4.6）。

    ``include_everyone`` / ``inject_default_dacl`` 是变异测试（M1/M4）的
    可测接缝 —— 生产路径两者恒为 True，变异测试构造缺少该保证的令牌以
    钉住对应机制的必要性。
    """
    _require()
    current = open_current_process_token()
    try:
        logon = find_logon_sid(current)
        everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid)
        # policy 产出 SID 字符串；CreateRestrictedToken 需要 PySID 对象
        def _to_pysid(s):
            return (win32security.ConvertStringSidToSid(s)
                    if isinstance(s, str) else s)

        restricting = [(s, 0) for s in (logon, *[_to_pysid(x) for x in write_sids])]
        if include_everyone:
            restricting.append((everyone, 0))
        try:
            restricted = win32security.CreateRestrictedToken(
                current, TOKEN_FLAGS, [], [], restricting)
        except Exception as e:  # fail-closed
            raise SandboxUnavailableError(
                f"CreateRestrictedToken failed: {e}",
                api_name="CreateRestrictedToken")
    finally:
        current.Close()

    if inject_default_dacl:
        try:
            _inject_default_dacl(restricted, temp_sid)
        except Exception as e:
            try:
                restricted.Close()
            except Exception:
                pass
            raise SandboxUnavailableError(
                f"default DACL injection failed: {e}",
                api_name="SetTokenInformation(TokenDefaultDacl)")
    return restricted


def _inject_default_dacl(token, temp_sid: str) -> None:
    """把 temp SID 的全权 ACE **合并**（非重建）进令牌默认 DACL（§4.5）。"""
    dacl = win32security.GetTokenInformation(token, win32security.TokenDefaultDacl)
    sid_obj = win32security.ConvertStringSidToSid(temp_sid)
    dacl.SetEntriesInAcl([{
        "AccessPermissions": FILE_ALL_ACCESS,
        "AccessMode": win32security.GRANT_ACCESS,
        "Inheritance": 0,
        "Trustee": {
            "MultipleTrustee": None,
            "MultipleTrusteeOperation": 0,
            "TrusteeForm": win32security.TRUSTEE_IS_SID,
            "TrusteeType": win32security.TRUSTEE_IS_UNKNOWN,
            "Identifier": sid_obj,
        },
    }])
    win32security.SetTokenInformation(token, win32security.TokenDefaultDacl, dacl)


class RestrictedTokenFactory:
    """受限令牌工厂。create() 返回可传给 CreateProcessAsUser 的 token 句柄。"""

    def create(self, write_sids: list[str], temp_sid: str):
        """构造 WRITE_RESTRICTED 令牌 + 默认 DACL 注入（§4.5，管道生死线）。

        restricting = [logon, Everyone, *write_sids]（§4.6 保活组）。
        DisableSids / PrivilegesToDelete 恒空 —— 破坏"读不设限"承诺。
        """
        return _create_restricted_token(write_sids, temp_sid)
