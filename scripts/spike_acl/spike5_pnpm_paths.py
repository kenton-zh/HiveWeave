"""Spike 5：pnpm + S8 路径盲区 + pwsh node 孙进程修正。"""

import base64
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402
from common import (  # noqa: E402
    confined_spawn, create_restricted_token, grant_write,
    workspace_write_sid, temp_write_sid,
)
import win32security  # noqa: E402

RESULTS = []
BASE = Path(r"D:\PC_AI\Project\acl-spike-run")
WS = BASE / "ws"
TEMP_PRIVATE = WS / ".sandbox-temp"
CACHE = WS / ".hiveweave-cache"
PNPM = r"C:\Users\99744\AppData\Roaming\npm\pnpm.cmd"


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'[PASS]' if ok else '[FAIL]'} {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    WS.mkdir(parents=True, exist_ok=True)
    TEMP_PRIVATE.mkdir(exist_ok=True)
    (CACHE / "pnpm-store").mkdir(parents=True, exist_ok=True)
    (CACHE / "pnpm").mkdir(parents=True, exist_ok=True)

    ws_sid_str = workspace_write_sid(str(WS))
    t_sid_str = temp_write_sid(str(TEMP_PRIVATE))
    grant_write(str(WS), ws_sid_str, 0x1301DF)
    grant_write(str(TEMP_PRIVATE), t_sid_str, 0x1301DF)
    ws_sid = win32security.ConvertStringSidToSid(ws_sid_str)
    t_sid = win32security.ConvertStringSidToSid(t_sid_str)
    token, _ = create_restricted_token([ws_sid], t_sid)

    env = {
        "SystemRoot": os.environ["SystemRoot"],
        "PATH": os.environ.get("PATH", ""),
        "TEMP": str(TEMP_PRIVATE),
        "TMP": str(TEMP_PRIVATE),
        "USERPROFILE": r"C:\Users\99744",
        "NPM_CONFIG_CACHE": str(CACHE / "npm"),
        "APPDATA": str(CACHE / "appdata"),
        # pnpm：store 与缓存都指进 workspace（R6 变量名实测点）
        "npm_config_store_dir": str(CACHE / "pnpm-store"),
        "PNPM_HOME": str(CACHE / "pnpm"),
        "XDG_DATA_HOME": str(CACHE / "xdg"),
    }
    (CACHE / "appdata").mkdir(exist_ok=True)
    (CACHE / "xdg").mkdir(exist_ok=True)

    # ── pnpm 生死测试 ──
    pkg_dir = WS / "pnpm-probe"
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir, ignore_errors=True)
    pkg_dir.mkdir()
    (pkg_dir / "package.json").write_text(
        '{"name": "probe", "version": "1.0.0"}', encoding="utf-8")
    r = confined_spawn(
        token, f'"{PNPM}" install --store-dir "{CACHE / "pnpm-store"}" left-pad@1.3.0',
        str(pkg_dir), env=env, timeout_s=180)
    check("S2.pnpm_install",
          r["exit_code"] == 0 and (pkg_dir / "node_modules" / "left-pad").exists(),
          f"exit={r['exit_code']} err={r['stderr'][:250]!r}")

    # pnpm store 位置断言（R6：防静默回退全局 store）
    if r["exit_code"] == 0:
        r2 = confined_spawn(
            token, f'"{PNPM}" store path',
            str(pkg_dir), env=env, timeout_s=30)
        in_ws = str(CACHE / "pnpm-store").lower() in r2["stdout"].lower()
        check("S2.pnpm_store_in_workspace", in_ws, f"out={r2['stdout'][:80]!r}")

    # ── pwsh node 孙进程（引号修正：base64 EncodedCommand） ──
    ps_script = (
        "$out = & 'C:\\Program Files\\nodejs\\node.exe' -e "
        "\"console.log('node-via-pwsh-ok')\" 2>&1 | Out-String; "
        "Write-Output $out")
    b64 = base64.b64encode(ps_script.encode("utf-16-le")).decode()
    r = confined_spawn(
        token, f"powershell -NoProfile -EncodedCommand {b64}",
        str(WS), env=env, timeout_s=60)
    check("pwsh.node_grandchild",
          r["exit_code"] == 0 and "node-via-pwsh-ok" in r["stdout"],
          f"out={r['stdout'][:80]!r} err={r['stderr'][:150]!r}")

    # ── S8: 路径盲区 ──
    # 8.3 短名 workspace（PROGRA~1 风格）
    short_ws = Path(r"D:\PC_AI\Project\acl-spike-run\ws")  # 基准
    sid_from_short = workspace_write_sid(r"D:\PC_AI\Project\ACL-S~1\ws") if False else None
    # 手工造一个短名：D:\PC_AI\Project\acl-sp~1 可能存在——用 fsutil/dir /x 查
    r3 = confined_spawn(
        token, f'cmd /c "for %I in (\"{BASE}\") do @echo %~sI"',
        str(WS), env=env, timeout_s=15)
    short_path = r3["stdout"].strip()
    check("S8.shortname_query", r3["exit_code"] == 0 and "~" in short_path,
          f"short={short_path!r}")
    if "~" in short_path:
        sid_short = workspace_write_sid(short_path)
        sid_full = workspace_write_sid(str(BASE))
        check("S8.shortname_realpath_converges", sid_short == sid_full,
              f"short={sid_short} full={sid_full}")

    # 非 ASCII workspace
    uni_dir = BASE / "中文目录"
    uni_dir.mkdir(exist_ok=True)
    try:
        uni_sid = workspace_write_sid(str(uni_dir))
        grant_write(str(uni_dir), uni_sid, 0x1301DF)
        uni_sid_obj = win32security.ConvertStringSidToSid(uni_sid)
        token2, _ = create_restricted_token([uni_sid_obj], t_sid)
        r = confined_spawn(
            token2, f'cmd /c echo u > "{uni_dir}\\u.txt"', str(uni_dir),
            env=env, timeout_s=20)
        check("S8.non_ascii_workspace",
              r["exit_code"] == 0 and (uni_dir / "u.txt").exists(),
              f"err={r['stderr'][:100]!r}")
        token2.Close()
    except Exception as e:
        check("S8.non_ascii_workspace", False, repr(e))

    # 长路径 > 260
    deep = WS
    try:
        for i in range(8):
            deep = deep / f"very-long-directory-name-{i:02d}-padding-padding"
        deep.mkdir(parents=True, exist_ok=True)
        if len(str(deep)) > 260:
            r = confined_spawn(
                token, f'cmd /c echo deep > "{deep}\\d.txt"', str(WS),
                env=env, timeout_s=20)
            check("S8.longpath_write", (deep / "d.txt").exists(),
                  f"len={len(str(deep))} err={r['stderr'][:100]!r}")
        else:
            check("S8.longpath_write", True, f"path len {len(str(deep))} < 260, skipped")
    except Exception as e:
        check("S8.longpath_write", False, repr(e))

    token.Close()
    print("\n=== Spike5 summary ===")
    fails = [x for x in RESULTS if not x[1]]
    print(f"total={len(RESULTS)} pass={len(RESULTS)-len(fails)} fail={len(fails)}")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  FAIL: {n} {d}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
