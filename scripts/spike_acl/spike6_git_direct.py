"""补充 spike：git.exe 直跑（不经 bash）在受限令牌下的完整工作流。"""

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

WS = Path(r"D:\PC_AI\Project\acl-git-test")
shutil.rmtree(WS, ignore_errors=True)
WS.mkdir()
TEMP = WS / ".sandbox-temp"
TEMP.mkdir()

ws_sid_str = workspace_write_sid(str(WS))
t_sid_str = temp_write_sid(str(TEMP))
grant_write(str(WS), ws_sid_str)
grant_write(str(TEMP), t_sid_str)
ws_sid = win32security.ConvertStringSidToSid(ws_sid_str)
t_sid = win32security.ConvertStringSidToSid(t_sid_str)
token, _ = create_restricted_token([ws_sid], t_sid)

env = {
    "SystemRoot": os.environ["SystemRoot"],
    "PATH": os.environ.get("PATH", ""),
    "TEMP": str(TEMP), "TMP": str(TEMP),
    "USERPROFILE": r"C:\Users\99744",
    "HOME": r"C:\Users\99744",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}
GIT = r"C:\Program Files\Git\cmd\git.exe"


def run(name, args):
    r = confined_spawn(token, f'"{GIT}" {args}', str(WS), env=env, timeout_s=60)
    print(f"{name}: exit={r['exit_code']}"
          + (f" out={r['stdout'][:60]!r}" if r["stdout"].strip() else "")
          + (f" err={r['stderr'][:150]!r}" if r["stderr"].strip() else ""))
    return r


run("git init", "init -q .")
(WS / "f.txt").write_text("hi", encoding="utf-8")
run("git add", "add f.txt")
run("git commit", "commit -qm test")
run("git log", "log --oneline")
run("git status", "status --porcelain")
run("git diff HEAD", "diff HEAD --stat")
token.Close()
shutil.rmtree(WS, ignore_errors=True)
print("cleaned")
