"""Spike 2：端到端受限进程行为验证（S7b + S4 + 沙箱核心语义）。

S7b: CreateProcessAsUser 普通用户（自身受限副本）可行性
S4:  newEnvironment=dict 显式环境块传递
核心: 受限 cmd 的写边界 —— workspace 内写成功 / 外写 EACCES / 外读成功
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (  # noqa: E402
    confined_spawn, create_restricted_token, grant_write,
    workspace_write_sid, temp_write_sid,
)
import win32security  # noqa: E402

RESULTS = []
CMD = os.environ.get("COMSPEC", "cmd.exe")


def check(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    print(f"{'[PASS]' if ok else '[FAIL]'} {name}" + (f" — {detail}" if detail else ""))


def run_confined_cmd(token, command: str, cwd: str, env: dict):
    return confined_spawn(
        token, f'"{CMD}" /c {command}', cwd, env=env, timeout_s=30)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="acl-spike2-"))
    ws = tmp / "workspace"
    ws.mkdir()
    outside = tmp / "outside"
    outside.mkdir()
    private_temp = tmp / "temp-private"
    private_temp.mkdir()

    ws_sid_str = workspace_write_sid(str(ws))
    t_sid_str = temp_write_sid(str(private_temp))
    grant_write(str(ws), ws_sid_str)
    grant_write(str(private_temp), t_sid_str)

    ws_sid = win32security.ConvertStringSidToSid(ws_sid_str)
    t_sid = win32security.ConvertStringSidToSid(t_sid_str)
    token, _ = create_restricted_token([ws_sid], t_sid)

    # S4: 显式 env dict（含一个哨兵变量）
    env = {
        "SystemRoot": os.environ["SystemRoot"],
        "PATH": os.environ.get("PATH", ""),
        "TEMP": str(private_temp),
        "TMP": str(private_temp),
        "SPIKE_SENTINEL": "acl-42",
    }

    # ── S7b: spawn 本身 ──
    try:
        r = run_confined_cmd(token, "echo hello", str(ws), env)
        ok = r["exit_code"] == 0 and "hello" in r["stdout"]
        check("S7b.create_process_as_user", ok,
              f"exit={r['exit_code']} out={r['stdout'][:60]!r} err={r['stderr'][:120]!r}")
    except Exception as e:
        check("S7b.create_process_as_user", False, repr(e))
        return 1

    # ── S4: env dict 传递验证 ──
    r = run_confined_cmd(token, "echo %SPIKE_SENTINEL%", str(ws), env)
    check("S4.new_env_dict_passed", "acl-42" in r["stdout"],
          f"stdout={r['stdout'][:60]!r}")
    r = run_confined_cmd(token, "echo %TEMP%", str(ws), env)
    check("S4.temp_redirected", str(private_temp).lower() in r["stdout"].lower(),
          f"stdout={r['stdout'][:80]!r}")

    # ── 核心: workspace 内写 ──
    r = run_confined_cmd(token, f"echo data > {ws}\\inside.txt", str(ws), env)
    check("core.write_inside_workspace", r["exit_code"] == 0 and (ws / "inside.txt").exists(),
          f"exit={r['exit_code']} err={r['stderr'][:120]!r}")

    # ── 核心: workspace 外写被拒（pass-2 落空 → EACCES） ──
    outside_file = outside / "evil.txt"
    r = run_confined_cmd(token, f"echo pwned > {outside_file}", str(ws), env)
    check("core.write_outside_denied",
          r["exit_code"] != 0 and not outside_file.exists(),
          f"exit={r['exit_code']} err={r['stderr'][:150]!r}")

    # ── 核心: 私有 temp 可写 ──
    r = run_confined_cmd(token, "echo t > %TEMP%\\t.txt", str(ws), env)
    check("core.write_private_temp", r["exit_code"] == 0 and (private_temp / "t.txt").exists(),
          f"exit={r['exit_code']} err={r['stderr'][:120]!r}")

    # ── 核心: 读不设限（workspace 外读成功） ──
    (outside / "readable.txt").write_text("secret-content", encoding="utf-8")
    r = run_confined_cmd(token, f"type {outside}\\readable.txt", str(ws), env)
    check("core.read_outside_allowed",
          r["exit_code"] == 0 and "secret-content" in r["stdout"],
          f"exit={r['exit_code']} out={r['stdout'][:60]!r}")

    # ── 核心: 兄弟 temp 不可写（无该 temp SID ACE） ──
    sibling_temp = tmp / "temp-sibling"
    sibling_temp.mkdir()
    r = run_confined_cmd(token, f"echo x > {sibling_temp}\\s.txt", str(ws), env)
    check("core.write_sibling_temp_denied",
          r["exit_code"] != 0 and not (sibling_temp / "s.txt").exists(),
          f"exit={r['exit_code']}")

    # ── 核心: 越界删除 workspace 外文件被拒 ──
    victim = outside / "victim.txt"
    victim.write_text("keep-me", encoding="utf-8")
    r = run_confined_cmd(token, f"del {victim}", str(ws), env)
    check("core.delete_outside_denied",
          victim.exists() and "keep-me" in victim.read_text(encoding="utf-8"),
          f"exit={r['exit_code']}")

    # ── Job: 超时整树击杀 ──
    import time
    t0 = time.monotonic()
    r = confined_spawn(token, f'"{CMD}" /c ping -n 30 127.0.0.1 > nul',
                       str(ws), env=env, timeout_s=3)
    elapsed = time.monotonic() - t0
    check("job.timeout_tree_kill",
          r["timed_out"] and elapsed < 15,
          f"timed_out={r['timed_out']} elapsed={elapsed:.1f}s exit={r['exit_code']}")

    token.Close()
    print("\n=== Spike2 summary ===")
    fails = [x for x in RESULTS if not x[1]]
    print(f"total={len(RESULTS)} pass={len(RESULTS)-len(fails)} fail={len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
