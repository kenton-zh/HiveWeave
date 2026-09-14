"""Windows subprocess helpers — suppress console flash (SW_HIDE startupinfo).

Why ``STARTUPINFO.wShowWindow = SW_HIDE`` and NOT ``CREATE_NO_WINDOW``:

- ``CREATE_NO_WINDOW`` gives the direct child (e.g. ``cmd.exe``) NO console at
  all. When that child then spawns a console-subsystem grandchild (``node.exe``,
  ``bun.exe``, ``npx``/``.cmd`` shims, ``git.exe``), Windows allocates a BRAND
  NEW visible console for the grandchild — the flashing/persistent black
  console window users see (a long-lived dev server keeps it open forever).
- With ``STARTF_USESHOWWINDOW | SW_HIDE`` the direct child gets a hidden
  console (or attaches to the parent's existing console); console grandchildren
  INHERIT that hidden console, so no window ever appears for the whole process
  tree.

This module is the SINGLE spawn funnel for the whole repo (mirrors upstream
opencode's ``util/process.ts`` + ``cross-spawn-spawner.ts`` pattern): business
code MUST NOT call ``subprocess.*`` / ``asyncio.create_subprocess_*`` directly
— always go through ``hidden_run`` / ``hidden_popen`` / ``hidden_exec`` /
``hidden_shell`` below. ``tests/test_spawn_funnel_guard.py`` enforces this.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

# Re-exports so business code never needs ``import subprocess`` itself
# (tests/test_spawn_funnel_guard.py bans the import outside this module).
DEVNULL = subprocess.DEVNULL
PIPE = subprocess.PIPE
STDOUT = subprocess.STDOUT
CompletedProcess = subprocess.CompletedProcess
TimeoutExpired = subprocess.TimeoutExpired
Popen = subprocess.Popen  # 仅注解用；构造请走 hidden_popen
list2cmdline = subprocess.list2cmdline
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)


def _hidden_startupinfo() -> Any:
    """STARTUPINFO with a hidden console window (Windows only)."""
    si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    si.wShowWindow = 0  # SW_HIDE
    return si


def windows_no_window_kwargs() -> dict[str, Any]:
    """Kwargs for subprocess.Popen / asyncio.create_subprocess_* on Windows.

    Without a hidden console, every ``cmd /c``, ``git``, ``npm``, ``bun``,
    ``browse.exe`` flashes a black console window when agents run tools.
    """
    if not sys.platform.startswith("win"):
        return {}
    return {"startupinfo": _hidden_startupinfo()}


def merge_creationflags(*flags: int) -> int:
    """OR Windows creation flags (no-op on non-Windows).

    Deliberately does NOT add ``CREATE_NO_WINDOW``: it only hides the direct
    child while forcing console grandchildren to allocate new visible console
    windows. Window suppression is handled by ``windows_no_window_kwargs``
    (SW_HIDE startupinfo) instead.
    """
    if not sys.platform.startswith("win"):
        return 0
    out = 0
    for f in flags:
        out |= int(f or 0)
    return out

def _with_hidden_startupinfo(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Inject hidden-console startupinfo unless the caller supplied its own."""
    if not sys.platform.startswith("win"):
        return kwargs
    if kwargs.get("startupinfo") is not None:
        return kwargs
    out = dict(kwargs)
    out["startupinfo"] = _hidden_startupinfo()
    return out


# ── GitSpawn 加固（2026-09-14） ───────────────────────────────────────
#
# 威胁模型：agent 对 <project>/.git 持 GRANT_MASK 全权写（acl_sandbox/
# service.py `.git` 授予；grant.py GRANT_MASK 注释明写「授予写/删」），而
# 平台后端自己跑 git merge/commit/checkout/worktree —— 这些正是 hook /
# fsmonitor / attributes-driver 的触发点。⇒ 一个受限 executor 写
# .git/config，就能让**不受沙箱与 ACL 约束的平台进程**替它执行代码
# （CVE-2026-48124 同构模式：agent 写配置 → 沙箱外组件稍后执行）。
#
# 加固为什么住在**本模块**而不是 git_cmd._git()：本模块是全仓唯一 spawn
# 漏斗（test_spawn_funnel_guard.py 强制业务代码不得直接 spawn），因此
# 验收问句「能否写一个绕过它的调用方？」的答案是「绕过漏斗即违规」。
# 放这里一次覆盖平台侧全部 git 调用（含 service_create/service_sync/
# reconcile 等 30+ 处与 8 个绕过 _git() 的裸调用点），不需要逐点改。
#
# 为什么用环境变量而不是 `git -c`：`-c` 只作用于当次 argv，**无法穿透
# shell**。agent 的 bash/pwsh 工具 spawn 的 git 是孙进程，只有环境变量
# 能继承到（P1-1）。
#
# ⚠ 覆盖面必须说准（实测 git 2.55.0.windows.3，脚本在 .tmp-test/
# git_hardening_probe*.py；判据是「无加固时 EXECUTED、注入后 BLOCKED」）：
#   ✅ 已覆盖 4 类：core.hooksPath→commit、core.fsmonitor→status、
#      diff.external→diff、attr.tree→add（attr.tree 是「让 .gitattributes
#      从任意 tree 读」的跳板）。
#   ❌ **未覆盖 2 类**：filter.<name>.clean→add、merge.<name>.driver→merge。
#      无加固时 EXECUTED，注入本静态清单后**仍 EXECUTED** —— 原因见边界 ①，
#      测试以 xfail(strict=True) 如实登记，不粉饰、不声称已堵。
#   · 覆盖效果与键名大小写无关（local/注入两侧驼峰/小写 4 种组合全 BLOCKED）
#   · bash 工具传的是 env=env（白名单整段替换）⇒ 加固必须合并进 kwargs
#     ["env"]，只改 os.environ 会让 P1-1 静默失效
#
# ⚠ 本层是**止血不是治本**。两条已知边界：
#   ① 键名枚举天然有缺口：`filter.<任意名>.clean`、`merge.<任意名>.driver`
#      的 <任意名> 由仓库内 .gitattributes 动态指定，静态清单列不出来 ⇒
#      **这两类当前是敞的**。已实测排除两条替代方案：`core.attributesFile`
#      指向空文件挡不住仓库内 .gitattributes 引入的 filter（第一轮 4c 仍
#      EXECUTED）；也试不出可注入的通配键。只能靠收回 .git/config 写权限
#      （P2）治本。
#   ② 环境变量挡不住 agent 自己：它在 shell 里 Remove-Item Env:GIT_CONFIG_COUNT
#      再跑 git 即可绕过。但那次 git 跑在**它自己的沙箱内**，不是提权；
#      真正危险的是平台后端那次调用，而平台进程的环境变量 agent 碰不到。
#
# ⚠ 挂钩点不止本模块：agent 的 bash/pwsh 命令在沙箱开启时走
# ``acl_sandbox.spawn.ConfinedRunner``（CreateProcessAsUserW，**完全绕过
# subprocess**，故 test_spawn_funnel_guard.py 扫不到它 —— 这是**已排除项**
# 而非漏网之鱼：该路经 ``_build_sandbox_env`` 拿到了同一套加固键，见
# ``apply_git_hardening`` 的调用点）。漏掉那一层会让 P1-1 静默失效。

_GIT_EXEC_NAMES = frozenset({"git", "git.exe"})
# shell 入口：无法预知里面会不会跑 git ⇒ 一律加固（P1-1）
_SHELL_EXEC_NAMES = frozenset({
    "pwsh", "pwsh.exe", "powershell", "powershell.exe",
    "cmd", "cmd.exe", "bash", "bash.exe", "sh", "sh.exe",
    "zsh", "zsh.exe", "wsl", "wsl.exe",
})

# 值为「程序/命令」的配置键 —— 全部压成空（= 压过 local config 的同一键，
# 但不删除）。键名固定，可静态枚举；动态键名（filter.*/merge.*/diff.*）
# 见上方边界 ①。
_GIT_CLEAN_KEYS = (
    "core.sshCommand",   # 跑 ssh 时执行
    "core.askPass",      # 需要口令时执行
    "core.gitProxy",     # 代理程序
    "diff.external",     # git diff 时执行
    "gpg.program",       # 签名程序
    "credential.helper", # 需要凭据时执行（平台零 push/fetch/pull，实测确认）
    "attr.tree",         # 让 .gitattributes 从任意 tree 读 ⇒ 挂 filter 的跳板
)

_HOOKS_DIR: str | None = None
_HARDENING_PAIRS: list[tuple[str, str]] | None = None
# 幂等标记：让 apply_git_hardening 对同一 env 只生效一次（并让子进程能自证）
GIT_HARDENING_MARK = "HIVEWEAVE_GIT_HARDENED"


def _empty_hooks_dir() -> str | None:
    """平台空 hook 目录（正斜杠）。**必须 agent 不可写**。

    只用平台数据根（``get_data_root()``：脚本模式 = apps/hiveweave-py/data，
    冻结 EXE = <exe>/data）—— 它不在任何项目 ACL 授权面内（授权面只有项目根
    + 项目级 cache/venv + 项目 temp），故 agent 写不进去。

    **刻意不退到系统 temp**：系统 temp 是否落在受限令牌可写面内不由本模块
    保证，一旦可写就等于把 hooksPath 指向攻击者能放 hook 的目录（自开攻击
    面、且伪装成"已加固"）。拿不到数据根 ⇒ 返回 None，本次不注入 hooksPath
    （其余键照注入）、**失败不缓存**（下次 spawn 重试），绝不静默假装成功。
    """
    global _HOOKS_DIR
    if _HOOKS_DIR:
        return _HOOKS_DIR
    try:
        from pathlib import Path

        from hiveweave.config import get_data_root

        base = Path(get_data_root()) / "empty-git-hooks"
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    _HOOKS_DIR = str(base).replace("\\", "/")
    return _HOOKS_DIR


def git_hardening_pairs() -> list[tuple[str, str]]:
    """注入用的 (key, value) 对。路径一律正斜杠（git config 会吃掉反斜杠）。"""
    hooks = _empty_hooks_dir()
    pairs: list[tuple[str, str]] = [
        ("core.fsmonitor", "false"),  # 关掉「配置即执行」的头号路径
        ("core.editor", "true"),      # sh 内建 no-op；防交互式 git 卡死
        ("core.pager", "cat"),        # 非 tty 本不触发，显式钉住
    ]
    if hooks:
        # 必须指向**平台空目录**而不是空串：空串实测虽也 BLOCKED，但那是
        # git 把空路径解析成什么实现的巧合，语义上不可依赖；有目录才确定。
        pairs.insert(0, ("core.hooksPath", hooks))
    pairs.extend((k, "") for k in _GIT_CLEAN_KEYS)
    return pairs


def _hardening_pairs_cached() -> list[tuple[str, str]]:
    """缓存加固清单。

    **仅在 hooksPath 解析成功时写入缓存**：否则会把「一次失败（数据根不可用）」
    钉成进程级永久状态，与 `_empty_hooks_dir` 承诺的「失败不缓存、下次 spawn
    重试」自相矛盾（复审 2026-09-14 M3）。
    """
    global _HARDENING_PAIRS
    if _HARDENING_PAIRS is not None:
        return _HARDENING_PAIRS
    pairs = git_hardening_pairs()
    if any(key == "core.hooksPath" for key, _ in pairs):
        _HARDENING_PAIRS = pairs
    return pairs


def _count_base(env: dict[str, str]) -> int:
    """已有 GIT_CONFIG_COUNT 起点（非法/负 ⇒ 0）。"""
    try:
        start = int(env.get("GIT_CONFIG_COUNT") or 0)
    except (TypeError, ValueError):
        return 0
    return start if start > 0 else 0


def apply_git_hardening(env: dict[str, str]) -> dict[str, str]:
    """把 GIT_CONFIG_* 加固键**追加**进给定 env（返回新 dict，不就地改入参）。

    GIT_CONFIG_COUNT 是「槽位总数」语义（实测：天真独占会挤掉调用方已有的
    槽，如 `core.pager`）⇒ 必须追加到已有计数之后，不能直接覆盖。

    本函数是**跨挂钩点复用**的公开入口 —— 漏斗之外的 env 构造点也要接：
    ``acl_sandbox/service.py::_build_sandbox_env``（agent 受限命令的单一 env
    点，走 CreateProcessAsUserW，不经本模块）。不接这一层 ⇒ agent 自己的
    bash/pwsh 里跑的 git 仍是裸的（P1-1 静默失效）。
    """
    out = dict(env)
    # 幂等：同一个 env 被接两层（例如 shell 形态的漏斗 + 上层已调过本函数）
    # 时不得让 COUNT 翻倍 —— 标记同时让子进程可自证「本进程已受加固」。
    if out.get(GIT_HARDENING_MARK) == "1":
        return out
    out[GIT_HARDENING_MARK] = "1"
    start = _count_base(out)
    pairs = _hardening_pairs_cached()
    out["GIT_CONFIG_COUNT"] = str(start + len(pairs))
    for i, (key, value) in enumerate(pairs, start=start):
        out[f"GIT_CONFIG_KEY_{i}"] = key
        out[f"GIT_CONFIG_VALUE_{i}"] = value
    return out


def _argv_needs_git_hardening(args: tuple[Any, ...]) -> bool:
    """args 是 spawn 调用的实参元组（argv 或 shell 命令行）。"""
    if not args:
        return False
    head = args[0]
    if isinstance(head, (list, tuple)):
        return _argv_needs_git_hardening(tuple(head))
    if not isinstance(head, str):
        return False
    name = os.path.basename(head).lower()
    if name in _GIT_EXEC_NAMES or name in _SHELL_EXEC_NAMES:
        return True
    # 整条命令行（shell 形态）：首词按引号剥离后比对，再兜一层「命令里出现
    # git」——含 "C:/Program Files/Git/cmd/git.exe commit"（路径带空格，且
    # git 后紧跟 `.` 而非空格）这类形态。宁可多加固（只覆盖 git 配置、不改
    # git 正常行为），不可漏；误报只让一条无关命令多带几个环境变量。
    if len(args) == 1 and (" " in head or "\t" in head):
        first = head.split(None, 1)[0].strip("\"'")
        if os.path.basename(first).lower() in (_GIT_EXEC_NAMES | _SHELL_EXEC_NAMES):
            return True
        return "git" in head.lower()
    return False


def _with_git_hardening(
    args: tuple[Any, ...], kwargs: dict[str, Any], *, always: bool = False
) -> dict[str, Any]:
    """把加固键合并进本次 spawn 的环境。

    加固条件：``always=True``（`hidden_shell`，一定走 shell）/ `shell=True`
    （`hidden_run`/`hidden_popen` 的 shell 形态，如 process_registry 的
    dev server）/ argv 首参是 git 或 shell 入口。
    走 shell 就一定加固 —— 命令行里跑不跑 git 无法预知。
    """
    if not (always or kwargs.get("shell") or _argv_needs_git_hardening(args)):
        return kwargs
    base = kwargs.get("env")
    out = dict(kwargs)
    out["env"] = apply_git_hardening(
        dict(base) if base is not None else dict(os.environ)
    )
    return out


def hidden_run(*args: Any, **kwargs: Any) -> Any:
    """subprocess.run with forced hidden console on Windows."""
    import subprocess

    kwargs = _with_git_hardening(args, kwargs)
    return subprocess.run(*args, **_with_hidden_startupinfo(kwargs))


def hidden_popen(*args: Any, **kwargs: Any) -> Any:
    """subprocess.Popen with forced hidden console on Windows."""
    import subprocess

    kwargs = _with_git_hardening(args, kwargs)
    return subprocess.Popen(*args, **_with_hidden_startupinfo(kwargs))


async def hidden_exec(*args: Any, **kwargs: Any) -> Any:
    """asyncio.create_subprocess_exec with forced hidden console on Windows."""
    import asyncio

    kwargs = _with_git_hardening(args, kwargs)
    return await asyncio.create_subprocess_exec(*args, **_with_hidden_startupinfo(kwargs))


async def hidden_shell(*args: Any, **kwargs: Any) -> Any:
    """asyncio.create_subprocess_shell with forced hidden console on Windows.

    一定走 shell ⇒ 里面跑不跑 git 不可预知，**无条件加固**（always=True）。
    """
    import asyncio

    kwargs = _with_git_hardening(args, kwargs, always=True)
    return await asyncio.create_subprocess_shell(*args, **_with_hidden_startupinfo(kwargs))
