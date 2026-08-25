"""ACL 沙箱 ↔ 工具/平台接线共享助手（spec §5.7 六入口，P1）。

把「是否启用」「受限 shell 命令行」「项目根解析」集中在此，bash.py /
game_time.py 只做薄接线。shell 方言适配 = P1 最大工作量（S1：Git Bash
受限下不可用，受限 shell = pwsh 优先 + cmd 兜底）。
"""

from __future__ import annotations

import os
import shutil
import sys

import structlog

log = structlog.get_logger(__name__)


def acl_sandbox_active() -> bool:
    """沙箱启用判定：HIVEWEAVE_ACL_SANDBOX=on 且 Windows。"""
    from hiveweave.config import settings

    return bool(settings.acl_sandbox) and sys.platform.startswith("win")


def _quote_windows_arg(arg: str) -> str:
    """单个 argv 元素的 Windows 引号（CommandLineToArgvW / list2cmdline 规则）。

    含空格/引号/反斜杠即整体加引号，并按标准算法转义：
    - 引号前的反斜杠翻倍再补一个（``\\"`` → 字面引号）；
    - 行尾反斜杠翻倍（避免与闭合引号合成转义）；
    - 其余反斜杠原样（2n 段不再被目标进程减半）。

    这正是 E10 修剥引号根因的核心：每个参数独立引用，用户输入里的
    引号/空格/UNC 反斜杠不再被整串命令行的外层解析误剥。
    """
    if arg == "":
        return '""'
    if not any(c.isspace() for c in arg) and '"' not in arg and "\\" not in arg:
        return arg
    out: list[str] = ['"']
    i = 0
    n = len(arg)
    while i < n:
        backslashes = 0
        while i < n and arg[i] == "\\":
            backslashes += 1
            i += 1
        if i == n:
            # 行尾反斜杠：翻倍（闭合引号前不参与转义）
            out.append("\\" * (backslashes * 2))
        elif arg[i] == '"':
            out.append("\\" * (backslashes * 2 + 1))
            out.append('"')
            i += 1
        else:
            out.append("\\" * backslashes)
            out.append(arg[i])
            i += 1
    out.append('"')
    return "".join(out)


def quote_windows_argv(argv: list[str]) -> str:
    """argv 数组 → CreateProcessAsUserW 可直收的命令行（逐元素独立引用）。"""
    return " ".join(_quote_windows_arg(a) for a in argv)


def build_confined_argv(command: str) -> list[str]:
    """把 bash 语法命令包装成受限 shell 的 argv 数组（E10 argv 化）。

    - pwsh 优先（受限下可用，S1 实测通过）：经 ``_normalize_for_pwsh`` 做
      bash 惯用法适配（§18.3 —— export/``${VAR}``/source/带 flag 的 unix 命令）；
    - cmd 兜底：``/s /c`` + unix→cmd 命令映射（``_normalize_command``）。

    与 ``build_confined_command`` 的区别：返回 argv 数组，由 spawn 侧用
    ``quote_windows_argv`` 逐元素引用 → 避免整串转发时外壳二次剥引号。
    """
    pwsh = shutil.which("pwsh")
    if pwsh:
        from hiveweave.tools.bash import _normalize_for_pwsh  # 惰性，避免循环导入

        return [pwsh, "-NoProfile", "-NonInteractive", "-Command",
                _normalize_for_pwsh(command)]
    cmd = os.environ.get("COMSPEC", "cmd.exe")
    from hiveweave.tools.bash import _normalize_command  # 惰性，避免循环导入

    return [cmd, "/s", "/c", _normalize_command(command)]


def build_confined_command(command: str) -> str:
    """把 bash 语法命令包装成受限 shell 的命令行（CreateProcessAsUserW 直收）。

    E10 后为 argv 化结果的字符串形态（逐元素引用），保留给仅接受字符串的
    调用方（sentinel / game_time）；新调用方优先用 ``build_confined_argv``。
    """
    return quote_windows_argv(build_confined_argv(command))


async def resolve_project_root(project_id: str | None) -> str | None:
    """项目根 workspace（git/cache SID 派生源，§4.8/§8）。

    解析失败（无 project_id / 查询异常 / 未绑定）返回 None 并打告警 ——
    调用方应 fail-closed 而非静默把 worktree 当项目根（否则会建 per-worktree
    缓存且 git SID 授错目标）。
    """
    if not project_id:
        return None
    try:
        from hiveweave.services.worktree_review import project_main_workspace

        root = await project_main_workspace(project_id)
        if not root:
            log.warning(
                "acl_sandbox_project_root_unresolved",
                project_id=project_id,
                reason="no workspace binding",
            )
        return root
    except Exception as e:
        log.warning(
            "acl_sandbox_project_root_resolve_failed",
            project_id=project_id,
            error=str(e),
        )
        return None


async def project_sandbox_mode(project_id: str | None) -> str:
    """P3 (§9)：项目级 sandbox_mode —— ``danger-full-access`` = 逃生门（跳过沙箱）。

    无 project_id / 查询失败 / 未配置 → 返回 ``""``（继承 env，即默认启用）。
    """
    if not project_id:
        return ""
    try:
        from hiveweave.db import meta as meta_db

        row = await meta_db.query_one(
            "SELECT sandbox_mode FROM projects WHERE id = ?", [project_id]
        )
        if row is None:
            return ""
        return str(row["sandbox_mode"] or "").strip().lower()
    except Exception:
        return ""


async def fetch_additional_writable_dirs(project_root: str) -> list[str]:
    """P2 (§5.5b②)：项目级配置的附加可写目录（projects.additional_writable_dirs）。

    大小写不敏感匹配 workspace_path（Windows）。无配置/查询失败 → 空列表。
    调用方（spawn_confined）把结果作为 extra SID 组装进受限令牌 —— 空列表 =
    现状行为（不额外授予任何外部写）。
    """
    import json as _json
    import os as _os

    try:
        from hiveweave.db import meta as meta_db

        rows = await meta_db.query(
            "SELECT workspace_path, additional_writable_dirs FROM projects"
        )
        target = _os.path.normcase(_os.path.normpath(project_root))
        for r in rows:
            ws = r["workspace_path"] or ""
            if _os.path.normcase(_os.path.normpath(ws)) != target:
                continue
            raw = r["additional_writable_dirs"] or "[]"
            try:
                parsed = _json.loads(raw)
            except (ValueError, TypeError):
                return []
            return [str(d) for d in parsed if str(d).strip()]
    except Exception:
        return []
    return []
