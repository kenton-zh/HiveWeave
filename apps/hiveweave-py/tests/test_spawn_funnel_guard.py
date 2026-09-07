"""防退化守卫：spawn 只能走 util/win_subprocess.py 唯一漏斗。

对齐上游 opencode 的纪律（util/process.ts 是唯一 spawn 入口）：业务代码
禁止 ``import subprocess`` / ``from subprocess import ...``（常量与类型一律
从 win_subprocess 再导出取），禁止直接调 ``subprocess.*`` /
``asyncio.create_subprocess_*`` / ``os.system`` / ``os.popen`` / ``os.spawn*``
/ loop 级 ``subprocess_exec``。扫描 src/hiveweave 源码文本，命中即 fail
（注释行从宽跳过）。
"""

from __future__ import annotations

from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "hiveweave"

WHITELIST_FILES = {"util/win_subprocess.py"}

FORBIDDEN_TOKENS = (
    # import 面先封死：别名 import（as _sp）绕不过
    "import subprocess",
    "from subprocess import",
    # 再封调用面（白名单文件外的 Popen( 只可能是 from-import 后的裸调）
    "Popen(",
    "subprocess.run(",
    "subprocess.check_output(",
    "subprocess.check_call(",
    "subprocess.call(",
    "subprocess.getoutput(",
    "subprocess.getstatusoutput(",
    "asyncio.create_subprocess_exec(",
    "asyncio.create_subprocess_shell(",
    "asyncio.subprocess.create_subprocess_",
    "loop.subprocess_exec(",
    "loop.subprocess_shell(",
    "os.system(",
    "os.popen(",
    "os.spawn",
    "posix_spawn",
)


def test_no_raw_spawn_outside_funnel() -> None:
    violations: list[str] = []
    for py in sorted(SRC_DIR.rglob("*.py")):
        rel = py.relative_to(SRC_DIR).as_posix()
        if rel in WHITELIST_FILES:
            continue
        for lineno, line in enumerate(
            py.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if line.lstrip().startswith("#"):
                continue
            for token in FORBIDDEN_TOKENS:
                if token in line:
                    violations.append(f"{rel}:{lineno}: {token}")
    assert not violations, (
        "spawn 漏斗守卫命中（业务代码禁止直接 spawn / import subprocess，"
        "请改用 hiveweave.util.win_subprocess 的 hidden_run/hidden_popen/"
        "hidden_exec/hidden_shell，常量与类型从同模块再导出取）：\n"
        + "\n".join(violations)
    )
