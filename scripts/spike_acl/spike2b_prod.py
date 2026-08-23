"""Spike 2b（生产形态重制）+ S1 Git Bash：核心行为 + MSYS2 + git + node。

关键修正（spike 调试 + 子代理审计定稿结论）：
- workspace 必须位于带真实主体 ACE（用户 SID/AuthUsers）的用户常规目录——
  OWNER_RIGHTS-only 目录（Python tempfile 产物）的 owner 通道对
  write-restricted 令牌整体失效（pass-1 侧目录 owner 语义问题）
- 私有 temp 放 workspace 内部（路径全程在授权树内）
- GRANT_MASK 维持 DSH 原样 0x110156（无需加读位——pass-2 只作用于写；
  本脚本 0x1301DF 是调试期遗留值，数值上含读位无害，非必要条件）
"""

import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402
from common import (  # noqa: E402
    confined_spawn, create_restricted_token, grant_write,
    workspace_write_sid, temp_write_sid,
)
import win32security  # noqa: E402

RESULTS = []
CMD = os.environ.get("COMSPEC", "cmd.exe")
BASH = r"C:\Program Files\Git\bin\bash.exe"
NODE = r"C:\Program Files\nodejs\node.exe"
BASE = Path(r"D:\PC_AI\Project\acl-spike-run")
WS = BASE / "ws"
TEMP_PRIVATE = WS / ".sandbox-temp"


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'[PASS]' if ok else '[FAIL]'} {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    if BASE.exists():
        shutil.rmtree(BASE, ignore_errors=True)
    WS.mkdir(parents=True)
    TEMP_PRIVATE.mkdir()

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
        "HOMEDRIVE": "C:",
        "HOMEPATH": r"\Users\99744",
        "USERPROFILE": r"C:\Users\99744",
    }

    # ── 核心语义（cmd，生产形态） ──
    r = confined_spawn(token, f'"{CMD}" /c echo data > {WS}\\inside.txt',
                       str(WS), env=env, timeout_s=30)
    check("core.write_inside", r["exit_code"] == 0 and (WS / "inside.txt").exists())

    evil = r"C:\Users\99744\acl-evil2.txt"
    r = confined_spawn(token, f'"{CMD}" /c echo pwn > "{evil}"', str(WS), env=env,
                       timeout_s=30)
    check("core.write_outside_denied", r["exit_code"] != 0 and not Path(evil).exists())

    r = confined_spawn(token, f'"{CMD}" /c echo t > "%TEMP%\\t.txt"', str(WS),
                       env=env, timeout_s=30)
    check("core.write_private_temp",
          r["exit_code"] == 0 and (TEMP_PRIVATE / "t.txt").exists())

    readable = Path(r"C:\Users\99744\acl-readable.txt")
    readable.write_text("secret", encoding="utf-8")
    r = confined_spawn(token, f'"{CMD}" /c type C:\\Users\\99744\\acl-readable.txt',
                       str(WS), env=env, timeout_s=30)
    check("core.read_outside_allowed", "secret" in r["stdout"])
    readable.unlink()

    # ── S1: Git Bash (MSYS2) ──
    r = confined_spawn(token, f'"{BASH}" -c "echo msys2-ok"', str(WS), env=env,
                       timeout_s=30)
    check("S1.gitbash_echo", r["exit_code"] == 0 and "msys2-ok" in r["stdout"],
          f"err={r['stderr'][:100]!r}")

    r = confined_spawn(
        token, f'"{BASH}" -c "printf \\"a\\\\nb\\\\nc\\\\n\\" | grep b"',
        str(WS), env=env, timeout_s=30)
    check("S1.gitbash_pipe", r["exit_code"] == 0 and "b" in r["stdout"],
          f"out={r['stdout']!r} err={r['stderr'][:100]!r}")

    r = confined_spawn(
        token, f'"{BASH}" -c "echo x > {WS.as_posix()}/bash-file.txt"',
        str(WS), env=env, timeout_s=30)
    check("S1.gitbash_write_inside",
          r["exit_code"] == 0 and (WS / "bash-file.txt").exists(),
          f"err={r['stderr'][:100]!r}")

    r = confined_spawn(
        token, f'"{BASH}" -c "echo pwn > /c/Users/99744/acl-evil3.txt"',
        str(WS), env=env, timeout_s=30)
    check("S1.gitbash_write_outside_denied",
          r["exit_code"] != 0 and not Path(r"C:\Users\99744\acl-evil3.txt").exists())

    # ── S1b: git（MSYS2 runtime + .git 元数据） ──
    git_env = dict(env)
    r = confined_spawn(
        token, f'"{BASH}" -c "git init -q . && git config user.email s@t && '
               f'git config user.name s && echo hi > f.txt && git add f.txt && '
               f'git commit -qm test"', str(WS), env=git_env, timeout_s=60)
    check("S1.git_init_add_commit", r["exit_code"] == 0 and (WS / ".git").exists(),
          f"err={r['stderr'][:200]!r}")

    if (WS / ".git").exists():
        r = confined_spawn(token, f'"{BASH}" -c "git log --oneline | head -1"',
                           str(WS), env=git_env, timeout_s=30)
        check("S1.git_log", r["exit_code"] == 0 and "test" in r["stdout"],
              f"out={r['stdout'][:60]!r}")

    # ── S2 前哨: node ──
    r = confined_spawn(token, f'"{NODE}" -e "console.log(\'node-ok\')"', str(WS),
                       env=env, timeout_s=30)
    check("S2.node_hello", r["exit_code"] == 0 and "node-ok" in r["stdout"],
          f"err={r['stderr'][:150]!r}")

    # S2: node spawn 孙进程（named-pipe 边界的关键探针）
    r = confined_spawn(
        token,
        f'"{NODE}" -e "const c=require(\'child_process\').spawn(\'cmd\',[\'/c\',\'echo\',\'grandchild-ok\'],{{stdio:[\'ignore\',\'pipe\',\'pipe\']}});let o=\'\';c.stdout.on(\'data\',d=>o+=d);c.on(\'close\',code=>{{console.log(\'exit=\'+code+\' \'+o.trim());process.exit(code)}})"',
        str(WS), env=env, timeout_s=30)
    check("S2.node_piped_grandchild",
          r["exit_code"] == 0 and "grandchild-ok" in r["stdout"],
          f"out={r['stdout'][:120]!r} err={r['stderr'][:200]!r}")

    # Job: 超时整树击杀
    t0 = time.monotonic()
    r = confined_spawn(token, f'"{CMD}" /c ping -n 30 127.0.0.1 > nul', str(WS),
                       env=env, timeout_s=3)
    check("job.timeout_kill", r["timed_out"] and time.monotonic() - t0 < 15,
          f"timed_out={r['timed_out']}")

    token.Close()
    print("\n=== Spike2b+S1 summary ===")
    fails = [x for x in RESULTS if not x[1]]
    print(f"total={len(RESULTS)} pass={len(RESULTS)-len(fails)} fail={len(fails)}")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  FAIL: {n} {d}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
