"""Spike 4：npm install + 缓存重定向（S2 生死重测）+ node 孙进程修正。"""

import os
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
CACHE = WS / ".hiveweave-cache" / "npm"
NPM = r"C:\Program Files\nodejs\npm.cmd"
NODE = r"C:\Program Files\nodejs\node.exe"


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'[PASS]' if ok else '[FAIL]'} {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    WS.mkdir(parents=True, exist_ok=True)
    TEMP_PRIVATE.mkdir(exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

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
        # §8 缓存重定向（spec 预案）
        "NPM_CONFIG_CACHE": str(CACHE),
        "APPDATA": str(WS / ".hiveweave-cache" / "appdata"),
    }
    (WS / ".hiveweave-cache" / "appdata").mkdir(parents=True, exist_ok=True)

    # 1) npm install（缓存重定向后）
    pkg_dir = WS / "npm-probe"
    if pkg_dir.exists():
        import shutil
        shutil.rmtree(pkg_dir, ignore_errors=True)
    pkg_dir.mkdir()
    (pkg_dir / "package.json").write_text(
        '{"name": "probe", "version": "1.0.0"}', encoding="utf-8")
    r = confined_spawn(
        token, f'"{NPM}" install --no-audit --no-fund left-pad@1.3.0',
        str(pkg_dir), env=env, timeout_s=180)
    check("S2.npm_install_cachedir",
          r["exit_code"] == 0 and (pkg_dir / "node_modules" / "left-pad").exists(),
          f"exit={r['exit_code']} err={r['stderr'][:250]!r}")

    # 2) node 运行安装的包（完整链路）
    r = confined_spawn(
        token, f'"{NODE}" -e "console.log(require(\'left-pad\')(\'ok\', 5))"',
        str(pkg_dir), env=env, timeout_s=30)
    check("S2.node_require_pkg", r["exit_code"] == 0 and "  ok" in r["stdout"],
          f"out={r['stdout'][:60]!r} err={r['stderr'][:200]!r}")

    # 3) node spawn 孙进程 stdio=ignore（libuv 绕 pipe 验证）
    r = confined_spawn(
        token,
        f'"{NODE}" -e "const c=require(\'child_process\').spawn(\'cmd\',[\'/c\',\'exit\',\'0\'],{{stdio:\'ignore\'}});c.on(\'close\',()=>console.log(\'ignore-ok\'))"',
        str(WS), env=env, timeout_s=30)
    check("S2.node_spawn_ignore_stdio", r["exit_code"] == 0 and "ignore-ok" in r["stdout"],
          f"err={r['stderr'][:150]!r}")

    # 4) node spawn 孙进程 stdio=pipe（确认边界——预期 FAIL，记录行为）
    r = confined_spawn(
        token,
        f'"{NODE}" -e "try{{const c=require(\'child_process\').spawn(\'cmd\',[\'/c\',\'echo\',\'x\'],{{stdio:[\'ignore\',\'pipe\',\'pipe\']}});c.stdout.on(\'data\',()=>{{}});c.on(\'error\',e=>console.log(\'ERR:\'+e.code));c.on(\'close\',()=>console.log(\'done\'))}}catch(e){{console.log(\'THROW:\'+e.code)}}"',
        str(WS), env=env, timeout_s=30)
    pipe_denied = "EPERM" in r["stdout"]  # uv_spawn 同步 throw 走 catch 分支，exit=0
    check("S2.node_pipe_boundary_confirmed", pipe_denied,
          f"out={r['stdout'][:80]!r}")

    # 5) node spawn inherit（绕 pipe 的另一路径）
    r = confined_spawn(
        token,
        f'"{NODE}" -e "const c=require(\'child_process\').spawn(\'cmd\',[\'/c\',\'echo\',\'inh-ok\'],{{stdio:\'inherit\'}});c.on(\'close\',code=>process.exit(code))"',
        str(WS), env=env, timeout_s=30)
    check("S2.node_spawn_inherit", r["exit_code"] == 0 and "inh-ok" in r["stdout"],
          f"out={r['stdout'][:60]!r} err={r['stderr'][:150]!r}")

    token.Close()
    print("\n=== Spike4 summary ===")
    fails = [x for x in RESULTS if not x[1]]
    print(f"total={len(RESULTS)} pass={len(RESULTS)-len(fails)} fail={len(fails)}")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  FAIL: {n} {d}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
