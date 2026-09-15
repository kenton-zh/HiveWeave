"""run_git_seal_ab.py —— #2（收窄写面 + 封条 + 信任锚）的「修前 / 修后」对照运行器。

做两件事，产出可核验的对照日志：
  1. `git archive HEAD apps/hiveweave-py` 导出**纯净树**（= 未含本次改动的 src），
     以 `HW_SRC` 指过去跑两个探针 ⇒ 修前基线；
  2. 用工作区（含改动）再跑一遍 ⇒ 修后结果。

⚠ 必须先确认 `hiveweave.__file__` 真的落在目标树上 —— 本仓是 editable 安装，
不确认就可能两边跑的是同一份代码（那对照就是假的）。

用法（仓库根）：
    apps/hiveweave-py/.venv/Scripts/python.exe -u scripts/run_git_seal_ab.py
产出：/tmp/gitseal-before.log · /tmp/gitseal-after.log（+ 同目录 redirect 两份）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / "apps" / "hiveweave-py" / ".venv" / "Scripts" / "python.exe"
PROBES = ("probe_git_write_surface.py", "probe_git_trust_anchor.py")


def run(probe: str, src: Path | None, out: Path) -> int:
    env = dict(os.environ)
    if src is not None:
        env["HW_SRC"] = str(src)
    else:
        env.pop("HW_SRC", None)
    with out.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(
            [str(PY), "-u", str(REPO / "scripts" / probe)],
            cwd=str(REPO), env=env, stdout=fh, stderr=subprocess.STDOUT)
    return proc.returncode


def which_hiveweave(src: Path | None) -> str:
    env = dict(os.environ)
    if src is not None:
        env["HW_SRC"] = str(src)
    env["PYTHONPATH"] = str(src or (REPO / "apps" / "hiveweave-py" / "src"))
    proc = subprocess.run(
        [str(PY), "-c", "import hiveweave, sys; print(hiveweave.__file__)"],
        cwd=str(REPO), env=env, capture_output=True, text=True)
    return (proc.stdout or proc.stderr).strip().splitlines()[-1]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hw-pristine-"))
    tar = subprocess.run(
        ["git", "archive", "HEAD", "apps/hiveweave-py"],
        cwd=str(REPO), capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(tmp)], input=tar.stdout, check=True)
    pristine_src = tmp / "apps" / "hiveweave-py" / "src"

    print(f"# pristine tree = {tmp}")
    print(f"# before  loads: {which_hiveweave(pristine_src)}")
    print(f"# after   loads: {which_hiveweave(None)}")
    assert str(pristine_src) in which_hiveweave(pristine_src), \
        "HW_SRC 未被采用 —— editable 安装抢先，对照会失真"
    assert str(REPO / "apps" / "hiveweave-py" / "src") in which_hiveweave(None)

    outdir = Path(os.environ.get("TEMP", "/tmp"))
    for probe in PROBES:
        stem = probe.removesuffix(".py")
        rc = run(probe, pristine_src, outdir / f"{stem}-before.log")
        print(f"# BEFORE {probe} rc={rc}")
        rc = run(probe, None, outdir / f"{stem}-after.log")
        print(f"# AFTER  {probe} rc={rc}")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"# logs in {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
