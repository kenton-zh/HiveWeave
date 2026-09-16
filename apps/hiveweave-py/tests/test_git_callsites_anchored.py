"""#19：**所有** git spawn 落点必须接信任锚（AST 网）。

## 为什么需要一把网
`#2` 的信任锚（`services/git_worktree/git_anchor.py`）把「git 去哪找 gitdir /
common dir / 工作树」从**读 agent 可写文件**改成**平台派生**。但它只在
`git_cmd._git` / `_git_sync` / `conflict_predict._merge_tree` 里被接上；
其余地方只要自己 spawn 一个 `git` 就**绕过锚** ⇒ agent 写 `<wt>/.git` 指针或
`commondir` 就能让平台的 git 读它写的 config（实测形态见 `git_anchor` 的三条路径）。

`#19` 的原清单（fixqueue）列了 4 处。本网**第一次跑就多扫出两处**
（`tools/bash.py` 的 attestation HEAD 盖戳、`services/git_worktree/git_identity.py`
的 `retire_worktree_config`）—— 后者是"助手**作为值**传递"的写法
（`asyncio.to_thread(hidden_run, ["git", …])`），**早期的网看不见它**
（只认 `Call.func`）。这正是"靠人肉 grep / 窄判据会再漏"的证据。

## 判据（不是文案匹配）
扫 AST，命中任一形态即算一个「git spawn 落点」：
1. **直接调用**：`Call.func` 是 spawn 助手（`hidden_*` / `subprocess.*` / …）；
2. **助手作为值传递**：`Call` 的任一实参是 spawn 助手（`to_thread(hidden_run, …)`）；
且**该调用的某个实参**是 git argv 字面量（`"git"` / `"git.exe"` /
绝对路径里的 `…/git.exe` / `"git -C x …"` 这类整条命令行，或列表里含上述）。

白名单是**函数级**的（`file` → 允许的函数名 + 理由）：只允许 `_git` / `_git_sync`
这类**自带锚接线**的函数体内出现裸 spawn —— 文件级免检会让"同一文件日后新增
一条裸 spawn"继续绿（审计 §7）。

⚠ **本网不覆盖什么**（写清楚，免得"网绿"被读成"全接锚了"）：
- argv **完全由变量**拼出、且没有任何 git 字面量（如 `hidden_exec(*agent_argv)`）
  —— AST 拿不到字面量。这类落点的覆盖靠"新 spawn 一律走 `git_cmd`"的评审纪律；
- 非 git 的 spawn（那是 `tests/test_spawn_funnel_guard.py` 的面）；
- `asyncio.create_subprocess_*` 直调（同上，另网已禁）；
- `acl_sandbox` 的 `CreateProcessAsUserW`（**完全**绕过 `subprocess` ⇒ 两把网都
  扫不到；那是 **agent 自己的** git，env 已由 `_build_sandbox_env` 拿同一套加固
  —— 属**已排除项**，不是漏网）。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "hiveweave"

#: spawn 助手名。`Name` 形态直接比对；`Attribute` 形态还要看接收者白名单
#: （否则 `foo.run(["git"])` 这类无关调用会误报 —— 审计 §6 实测）。
_SPAWN_CALLS = frozenset({
    "hidden_run", "hidden_popen", "hidden_exec", "hidden_shell",
    "run", "Popen", "check_output", "check_call", "call",
    "create_subprocess_exec", "create_subprocess_shell",
})
#: `Attribute` 形态允许的接收者根名（`subprocess.run` / `ws.hidden_run` …）。
_ATTR_RECEIVERS = frozenset({
    "subprocess", "asyncio", "ws", "win_subprocess", "sp", "_sp",
})

#: 自带锚接线的落点：`file` → (允许的**函数**集合, 为什么允许)。
#: ⚠ **函数级**而不是文件级：文件级免检会让"同一文件日后新增裸 spawn"继续绿。
_ANCHORED_SITES: dict[str, tuple[frozenset[str], str]] = {
    "services/git_worktree/git_cmd.py": (
        frozenset({"_git", "_git_sync"}),
        "`_git`（async）与 `_git_sync`（sync 孪生体）—— 都先跑 `anchor_for_git`，"
        "AnchorRefusal ⇒ 返回 (False, 拒因) 且不回落裸 git。",
    ),
    "services/git_worktree/conflict_predict.py": (
        frozenset({"_merge_tree"}),
        "`git merge-tree` 不走 `_git`（要退出码），故 `_merge_tree` **无条件**先接 "
        "`anchor_for_git`（拒因 ⇒ 返回哨兵 -4 不跑）—— 实测确认不是分支上接。",
    ),
}


def _spawn_helper_name(node: ast.AST) -> str | None:
    """这个 AST 节点是不是「spawn 助手」？是 ⇒ 返回它的名字。"""
    if isinstance(node, ast.Name):
        return node.id if node.id in _SPAWN_CALLS else None
    if isinstance(node, ast.Attribute):
        if node.attr not in _SPAWN_CALLS:
            return None
        root: ast.AST = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id in _ATTR_RECEIVERS:
            return node.attr
        return None
    return None


def _is_git_token(value: str) -> bool:
    """字符串里有没有「git 可执行」这个词元（含 `…/git.exe`、`git -C x …`）。"""
    for tok in value.replace("\\", "/").strip().lower().split():
        base = tok.strip("\"'").rsplit("/", 1)[-1]
        if base in ("git", "git.exe"):
            return True
    return False


def _arg_is_git(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _is_git_token(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(_arg_is_git(e) for e in node.elts)
    if isinstance(node, ast.Starred):
        return _arg_is_git(node.value)
    return False


def _enclosing_function(tree: ast.AST) -> dict[int, str]:
    """行号 → 它所在的函数名（用于函数级白名单）。"""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if hasattr(sub, "lineno"):
                    # 内层函数会覆盖外层 —— 取最近的那个（walk 顺序上内层后到）
                    owner[sub.lineno] = node.name
    return owner


def _spawn_sites() -> dict[str, dict[str, list[int]]]:
    """全仓扫一遍 ⇒ {相对路径: {函数名: [行号, …]}}。"""
    found: dict[str, dict[str, list[int]]] = {}
    for py in sorted(SRC.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — 源码不该有语法错
            continue
        owner = _enclosing_function(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            # 形态①：直接调用 spawn 助手；形态②：助手**作为值**传递
            if _spawn_helper_name(node.func) is None and not any(
                _spawn_helper_name(a) is not None for a in node.args
            ):
                continue
            if not any(_arg_is_git(a) for a in node.args):
                continue
            rel = py.relative_to(SRC).as_posix()
            fn = owner.get(node.lineno, "<module>")
            found.setdefault(rel, {}).setdefault(fn, []).append(node.lineno)
    return found


def test_every_git_spawn_site_is_anchored():
    """★ #19 验收：裸 git spawn 只能出现在**白名单函数**体内。"""
    found = _spawn_sites()
    offenders: dict[str, list[str]] = {}
    for path, funcs in found.items():
        allowed, _why = _ANCHORED_SITES.get(path, (frozenset(), ""))
        bad = {fn: lines for fn, lines in funcs.items() if fn not in allowed}
        if bad:
            offenders[path] = [f"{fn}:{lines}" for fn, lines in bad.items()]
    assert not offenders, (
        "这些落点自己 spawn git 但**没接信任锚**（agent 写的 `<wt>/.git` / "
        f"`commondir` 会被平台的 git 读走）：{offenders}\n"
        "改走 `services/git_worktree.git_cmd._git`（async）或 `_git_sync`（sync），"
        f"并把函数名登记进 `_ANCHORED_SITES`（附理由）：{sorted(_ANCHORED_SITES)}"
    )


def test_net_actually_sees_the_anchored_sites():
    """反向对照①：网必须**看得见**白名单里那些函数。

    没有这条，`_spawn_sites()` 一旦坏掉（返回空）"零违规"就恒真 ——
    本仓在"扫描器自己失效"上栽过（棘轮那轮的覆盖网就是同一个形态）。
    """
    found = _spawn_sites()
    for path, (allowed, _why) in _ANCHORED_SITES.items():
        assert path in found, (
            f"扫描没看到 {path} 的 git spawn ⇒ 网自己失效了（不是「没有违规」）"
        )
        missing = allowed - set(found[path])
        assert not missing, f"{path} 里白名单函数没被扫到：{sorted(missing)}"


def test_net_sees_helper_passed_as_a_value():
    """反向对照②（审计必修 3 的现场）：`to_thread(hidden_run, ["git", …])` 形态。

    早期判据只认 `Call.func` ⇒ 这条**看不见**，而生产里真有这样一处
    （`git_identity.retire_worktree_config`，且它是 R3 收口的 fail-closed 前提）。
    用内联代码片段验证判据本身，而不是只验证"当前仓库恰好没有"。
    """
    snippet = ast.parse(
        'async def f():\n'
        '    await asyncio.to_thread(hidden_run, ["git", "config", "--get", "x"])\n'
    )
    calls = [n for n in ast.walk(snippet) if isinstance(n, ast.Call)]
    hit = [
        c for c in calls
        if any(_spawn_helper_name(a) is not None for a in c.args)
        and any(_arg_is_git(a) for a in c.args)
    ]
    assert hit, "「助手作为值传递」的形态又漏了"


def test_net_is_not_fooled_by_unrelated_run_calls():
    """反向对照③：`foo.run(["git"])` 这类**无关调用**不得误报（审计 §6）。"""
    snippet = ast.parse('foo.run(["git"])\nself.call(["git"])\n')
    calls = [n for n in ast.walk(snippet) if isinstance(n, ast.Call)]
    assert not [c for c in calls if _spawn_helper_name(c.func) is not None]


def test_net_recognizes_git_by_path_or_command_line():
    """反向对照④：`…/git.exe` 与整条命令行里的 git 词元都要认得出（审计 §6）。"""
    for value in (
        r"C:/Program Files/Git/cmd/git.exe",
        "./git",
        "git -C /tmp/x status",
        r"C:\Program Files\Git\git.exe",
    ):
        assert _is_git_token(value), value
    for value in ("node", "python3", "/usr/bin/node", "gitleaks"):
        assert not _is_git_token(value), value


def test_blind_spots_are_declared_and_true():
    """把"网覆盖不到什么"钉成可执行断言（免得"网绿"被读成"全接锚了"）。

    变量 argv（无 git 字面量）**确实**扫不到 —— 这是**边界声明**，不是缺陷登记。
    """
    assert not _arg_is_git(ast.parse("x").body[0].value)  # Name 形态
    assert not _arg_is_git(ast.parse("*args").body[0].value)  # Starred(Name)
    # 但「列表里含 git 字面量」必须认得出
    assert _arg_is_git(ast.parse('["git", "status"]').body[0].value)
