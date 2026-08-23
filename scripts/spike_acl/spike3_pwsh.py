"""Spike 3：降级阶梯验证 —— pwsh / cmd 在受限令牌下的完整能力。

S1 失败后 Git Bash 不可用（MSYS2 signal pipe = named pipe 固定 SD，pass-2 拒）。
本 spike 验证：pwsh echo/管道/孙进程 spawn/文件操作/外部写拒。
"""

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
NODE = r"C:\Program Files\nodejs\node.exe"
NPM = r"C:\Program Files\nodejs\npm.cmd"


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'[PASS]' if ok else '[FAIL]'} {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    WS.mkdir(parents=True, exist_ok=True)
    TEMP_PRIVATE.mkdir(exist_ok=True)
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
    }

    def pwsh(ps_script, timeout_s=60):
        # PowerShell -Command 传参注意引号；用 base64 编码最稳
        import base64
        b64 = base64.b64encode(ps_script.encode("utf-16-le")).decode()
        return confined_spawn(
            token, f'powershell -NoProfile -EncodedCommand {b64}',
            str(WS), env=env, timeout_s=timeout_s)

    # 1) 基本执行
    r = pwsh("'pwsh-ok'")
    check("pwsh.hello", r["exit_code"] == 0 and "pwsh-ok" in r["stdout"],
          f"err={r['stderr'][:150]!r}")

    # 2) 管道
    r = pwsh("'a','b','c' | Where-Object { $_ -eq 'b' }")
    check("pwsh.pipe", r["exit_code"] == 0 and "b" in r["stdout"],
          f"err={r['stderr'][:150]!r}")

    # 3) workspace 内写
    r = pwsh(f"Set-Content -Path '{WS}\\ps-file.txt' -Value 'content'")
    check("pwsh.write_inside",
          r["exit_code"] == 0 and (WS / "ps-file.txt").exists(),
          f"err={r['stderr'][:150]!r}")

    # 4) 外部写拒
    r = pwsh("Set-Content -Path 'C:\\Users\\99744\\acl-evil4.txt' -Value 'pwn'")
    check("pwsh.write_outside_denied",
          r["exit_code"] != 0 and not Path(r"C:\Users\99744\acl-evil4.txt").exists())

    # 5) 外部读
    readable = Path(r"C:\Users\99744\acl-r2.txt")
    readable.write_text("ps-secret", encoding="utf-8")
    r = pwsh(f"Get-Content 'C:\\Users\\99744\\acl-r2.txt'")
    check("pwsh.read_outside", "ps-secret" in r["stdout"])
    readable.unlink()

    # 6) 孙进程 spawn（PowerShell 的管道 stdio = 匿名管道 CreatePipe → 令牌默认 DACL 消费者）
    r = pwsh(
        "$p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','echo','grand-ok' "
        "-NoNewWindow -Wait -PassThru -RedirectStandardOutput '"
        + str(TEMP_PRIVATE / "gc.txt").replace("\\", "\\\\") + "'; "
        "Get-Content '" + str(TEMP_PRIVATE / "gc.txt").replace("\\", "\\\\") + "'")
    check("pwsh.grandchild_process", r["exit_code"] == 0 and "grand-ok" in r["stdout"],
          f"out={r['stdout'][:100]!r} err={r['stderr'][:200]!r}")

    # 7) node 经 pwsh 起孙进程 + inherit stdio（绕过 libuv pipe 的 workaround 验证）
    r = pwsh(
        "& 'C:\\Program Files\\nodejs\\node.exe' -e \"console.log('node-via-pwsh')\"")
    check("pwsh.node_grandchild", r["exit_code"] == 0 and "node-via-pwsh" in r["stdout"],
          f"err={r['stderr'][:150]!r}")

    # 8) npm --version（JS 工具链探针）
    r = confined_spawn(token, f'"{NPM}" --version', str(WS), env=env, timeout_s=60)
    check("S2.npm_version", r["exit_code"] == 0 and r["stdout"].strip().startswith("1"),
          f"out={r['stdout'][:60]!r} err={r['stderr'][:200]!r}")

    # 9) npm install 最小包（真实 JS 工具链生死测试）
    pkg_dir = WS / "npm-probe"
    pkg_dir.mkdir(exist_ok=True)
    (pkg_dir / "package.json").write_text(
        '{"name": "probe", "version": "1.0.0"}', encoding="utf-8")
    r = confined_spawn(
        token, f'"{NPM}" install --no-audit --no-fund left-pad@1.3.0',
        str(pkg_dir), env=env, timeout_s=180)
    check("S2.npm_install",
          r["exit_code"] == 0 and (pkg_dir / "node_modules" / "left-pad").exists(),
          f"exit={r['exit_code']} err={r['stderr'][:300]!r}")

    token.Close()
    print("\n=== Spike3 (pwsh fallback) summary ===")
    fails = [x for x in RESULTS if not x[1]]
    print(f"total={len(RESULTS)} pass={len(RESULTS)-len(fails)} fail={len(fails)}")
    for n, ok, d in RESULTS:
        if not ok:
            print(f"  FAIL: {n} {d}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
