"""Spike 1：pywin32 原语验证（S3 + S6 + S7a）。

S3: GetTokenInformation/SetTokenInformation(TokenDefaultDacl) 的 PyACL 形态
S6: PyACL.SetEntriesInAcl 方法形态 + 精确 ACE 比对 round-trip
S7a: OpenProcessToken(ASSIGN_PRIMARY) + CreateRestrictedToken 普通用户可行性
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (  # noqa: E402
    GRANT_MASK, ace_of, create_restricted_token, grant_write,
    open_current_process_token, revoke_write, workspace_write_sid,
    temp_write_sid,
)

import win32security  # noqa: E402

RESULTS = []


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"{'[PASS]' if ok else '[FAIL]'} {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="acl-spike1-"))

    # ── S7a: OpenProcessToken + logon SID 普通用户 ──
    try:
        token = open_current_process_token()
        token.Close()
        check("S7a.open_process_token(ASSIGN_PRIMARY)", True)
    except Exception as e:
        check("S7a.open_process_token(ASSIGN_PRIMARY)", False, repr(e))
        return 1

    # ── S6: SID 派生 + ConvertStringSidToSid round-trip ──
    sid_string = workspace_write_sid(str(tmp))
    sid = win32security.ConvertStringSidToSid(sid_string)
    from common import sid_str
    check("S6.sid_roundtrip", sid_str(sid) == sid_string, sid_string)

    # ── S6: 精确 ACE 跳过（grant 两次：第一次写入、第二次跳过） ──
    ws_dir = tmp / "ws"
    ws_dir.mkdir()
    try:
        wrote1 = grant_write(str(ws_dir), sid_string)
        wrote2 = grant_write(str(ws_dir), sid_string)
        sd = win32security.GetNamedSecurityInfo(
            str(ws_dir), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        found = ace_of(dacl, sid_string, GRANT_MASK)
        check("S6.grant_write_first", wrote1 is True)
        check("S6.grant_exact_ace_skip", wrote2 is False,
              "second grant skipped (no re-propagation)")
        check("S6.ace_readable", found is not None, f"aces={dacl.GetAceCount()}")
        # 子文件继承验证（OI/CI 传播到新文件）
        (ws_dir / "probe.txt").write_text("x", encoding="utf-8")
        fsd = win32security.GetNamedSecurityInfo(
            str(ws_dir / "probe.txt"), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION)
        inherited = ace_of(fsd.GetSecurityDescriptorDacl(), sid_string, GRANT_MASK)
        check("S6.ace_inherited_by_new_file", inherited is not None)
        revoke_write(str(ws_dir), sid_string)
        sd2 = win32security.GetNamedSecurityInfo(
            str(ws_dir), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION)
        check("S6.revoke", ace_of(sd2.GetSecurityDescriptorDacl(), sid_string,
                                  GRANT_MASK) is None)
    except Exception as e:
        check("S6.grant_cycle", False, repr(e))

    # ── S3: TokenDefaultDacl 注入 + CreateRestrictedToken ──
    try:
        ws_sid = win32security.ConvertStringSidToSid(sid_string)
        temp_dir = tmp / "temp-private"
        temp_dir.mkdir()
        t_sid = win32security.ConvertStringSidToSid(temp_write_sid(str(temp_dir)))
        restricted, injected = create_restricted_token([ws_sid], t_sid)
        check("S3.create_restricted_token", True)

        new_dacl = win32security.GetTokenInformation(
            restricted, win32security.TokenDefaultDacl)
        injected_found = any(
            sid_str(ace[2]) == sid_str(injected)
            and (ace[1] & 0xFFFFFFFF) == 0x1F01FF
            for ace in (new_dacl.GetAce(i) for i in range(new_dacl.GetAceCount()))
        )
        check("S3.default_dacl_injection_persisted", injected_found,
              f"injected={sid_str(injected)}")

        restricted_sids = win32security.GetTokenInformation(
            restricted, win32security.TokenRestrictedSids)
        r_list = [sid_str(s) for s, _ in restricted_sids]
        check("S3.restricting_sids_present",
              sid_str(ws_sid) in r_list and sid_str(t_sid) in r_list,
              f"restricted={r_list}")
        restricted.Close()
    except Exception as e:
        check("S3.token_default_dacl", False, repr(e))

    print("\n=== Spike1 summary ===")
    fails = [r for r in RESULTS if not r[1]]
    print(f"total={len(RESULTS)} pass={len(RESULTS)-len(fails)} fail={len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
