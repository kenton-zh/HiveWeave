"""GitSpawn 加固守卫：agent 写 .git/config 不得让平台进程执行其代码。

背景（deliverables/security-gitspawn-hiveweave-exposure-2026-09-14.html）：
agent 对 <project>/.git 有 GRANT_MASK 全权写，平台后端自己跑 git
merge/commit/checkout/worktree ⇒ agent 写 .git/config 即可让不受沙箱约束的
平台进程执行它指定的代码（CVE-2026-48124 同构）。加固落在 util/win_subprocess.py
（全仓唯一 spawn 漏斗）。

本文件三层：
  1. 单元层 —— 命令识别 + GIT_CONFIG_COUNT 追加语义（不 mock，纯函数）
  2. **穿透层 —— 真子进程**：构造真实攻击仓库，经漏斗跑 git，断言 hook/driver
     未被执行。这一层不 mock spawn（对齐既有测试纪律：涉及子进程的必须真跑）
  3. 收口层 —— 报告列的 8 个 `_git()` 绕过点的真实调用形态必须被判为「需加固」

⚠ 每条断言都配阳性对照：`test_positive_control_*` 把加固清单清空后同一构造**必须
转红**。没有这条，「BLOCKED」可能只是探针盲区（本仓踩过：第一次扫描的「零命中」
实为探针缺陷）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src" / "hiveweave"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hiveweave.util import win_subprocess as ws  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not on PATH"
)


# ── 夹具 ────────────────────────────────────────────────────────────
def _raw_git(repo: Path, *args: str):
    """不经漏斗跑 git（建攻击现场用）—— 测试代码不受 src 漏斗守卫约束。"""
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )


def _funnel_git(repo: Path, *args: str, env: dict[str, str] | None = None):
    """**经漏斗**跑 git —— 这就是被测路径（平台与 agent 都只能走这里）。"""
    kwargs: dict = {
        "cwd": str(repo),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 60,
    }
    if env is not None:
        kwargs["env"] = env
    return ws.hidden_run(["git", *args], **kwargs)


def _whitelist_env() -> dict[str, str]:
    """模拟 tools/bash.py 的白名单 env（**整段替换**，不继承 os.environ）。

    这本身就是被测性质：加固必须合并进 kwargs["env"]；只改 os.environ 会让
    agent 的 shell 工具静默失守（P1-1）。
    """
    keep = {}
    for key in ("PATH", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP",
                "USERPROFILE", "PATHEXT", "COMSPEC"):
        val = os.environ.get(key)
        if val:
            keep[key] = val
    return keep


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "r"
    d.mkdir()
    _raw_git(d, "init", "-q")
    _raw_git(d, "config", "user.email", "t@t.t")
    _raw_git(d, "config", "user.name", "t")
    _raw_git(d, "config", "commit.gpgsign", "false")
    (d / "a.txt").write_text("a\n", encoding="utf-8")
    _raw_git(d, "add", "-A")
    _raw_git(d, "commit", "-q", "-m", "init")
    return d


def _sh_payload(flag: Path) -> Path:
    p = flag.parent / "payload.cmd"
    p.write_text(f"@echo off\r\necho hit > \"{flag}\"\r\n", encoding="utf-8", newline="")
    return p


def _fwd(p: Path) -> str:
    """git config 会吃掉反斜杠 ⇒ 一律正斜杠（实测副产品）。"""
    return str(p).replace("\\", "/")


def _hit(flag: Path, reset: bool = False) -> bool:
    got = flag.exists()
    if reset and got:
        flag.unlink()
    return got


# ══════════════════════════════════════════════════════════════════════
# 1. 单元层
# ══════════════════════════════════════════════════════════════════════
def test_identifies_git_and_shell_commands() -> None:
    """git 与 shell 入口都必须加固（shell 里可能跑 git = P1-1）。"""
    cases_true = [
        ("git", "status"),
        ("git.exe", "status"),
        (r"C:\Program Files\Git\cmd\git.exe", "rev-parse", "HEAD"),
        (["git", "commit", "-m", "x"],),
        (["C:/x/git.exe", "status"],),
        ("pwsh", "-NoProfile", "-Command", "Get-Date"),
        ("pwsh.exe", "-c", "x"),
        ("cmd", "/s", "/c", "x"),
        ("bash", "-c", "git status"),
        ("git status && echo done",),  # shell 整条命令行
    ]
    for case in cases_true:
        assert ws._argv_needs_git_hardening(case) is True, case

    cases_false = [
        (),
        ("python", "-c", "print(1)"),
        ("node", "index.js"),
        ("uv", "run", "pytest"),
        ("echo", "hello"),
        (None,),
        (123,),
    ]
    for case in cases_false:
        assert ws._argv_needs_git_hardening(case) is False, case


def test_hardening_pairs_cover_the_executable_valued_keys() -> None:
    """清单必须含报告点名的键，且 hooksPath 指向**平台空目录**（非空串）。

    #23（2026-09-16）改口径：键按 **git 的空值语义** 分两类 ——
    「空 = 禁用」注入空串；「空 = 不安全」（git 把空串读成"程序的名字是空的"
    ⇒ 真的去 spawn）注入 `"true"`（存在的 no-op）。判据是空值语义，
    **不是**触发面宽窄。
    """
    pairs = dict(ws.git_hardening_pairs())
    assert pairs["core.fsmonitor"] == "false"
    assert pairs["core.hooksPath"], "hooksPath 不能为空串（语义不可靠）"
    assert pairs["core.hooksPath"] != ""
    assert "\\" not in pairs["core.hooksPath"], "必须正斜杠（git config 吃反斜杠）"
    assert Path(pairs["core.hooksPath"]).is_dir(), "空 hook 目录必须真实存在"

    # ①「空 = 禁用」：注入空串
    for key in ("credential.helper", "attr.tree"):
        assert key in pairs, key
        assert pairs[key] == "", key
    # ②「空 = 不安全」：注入 no-op 程序，**不得**是空串
    for key in ("core.sshCommand", "core.askPass", "core.gitProxy",
                "gpg.program"):
        assert key in pairs, key
        assert pairs[key] == "true", (key, pairs[key])
    # ③ 刻意移除：`diff.external`
    assert "diff.external" not in pairs, (
        "diff.external 已被 #23 刻意移出注入清单 —— 注入空串会让 git 去 spawn "
        "一个名字为空的程序（rc=128），而 `true` 又会吞掉 diff 输出（拿不到 @@）；"
        "该键的防御已移交给 #2 的 .git/config 锁 + anchored _git。"
        "要加回来请先读 util/win_subprocess.py 的模块注释与 fixqueue #23。"
    )


def test_hardening_key_classes_make_the_two_semantics_explicit() -> None:
    """两类（+ 已移除一类）必须是**显式分列**的常量，不许合并成一个元组。

    为什么单独一条：合并成一个元组正是 #23 的成因 —— 当时 7 个键**用同一种
    方式**注入空串，于是「空 = 禁用」与「空 = 不安全」在代码上不可区分，
    只能靠人记住。这条用例让"顺手并回去"变成一次转红。
    """
    assert not hasattr(ws, "_GIT_CLEAN_KEYS"), (
        "_GIT_CLEAN_KEYS 已被拆成按空值语义分的两组（#23）—— 别再建同名聚合常量"
    )
    empt = set(ws._GIT_CLEAN_KEYS_EMPTY_DISABLES)
    noop = set(ws._GIT_CLEAN_KEYS_NOOP_PROGRAM)
    removed = set(ws._GIT_CLEAN_KEYS_REMOVED_BY_DESIGN)
    assert not (empt & noop), empt & noop
    assert not (empt & removed) and not (noop & removed)
    assert removed == {"diff.external"}
    # 三类合起来覆盖报告点名的 7 个键（既不能悄悄丢，也不能悄悄加）
    assert empt | noop | removed == {
        "core.sshCommand", "core.askPass", "core.gitProxy", "diff.external",
        "gpg.program", "credential.helper", "attr.tree",
    }


def test_hooks_dir_is_outside_any_project_surface() -> None:
    """空 hook 目录必须 agent 写不进去 —— 必须在平台数据根下。

    ACL 授权面只有「项目根 + 项目级 cache/venv/temp」，故平台数据根下的
    目录不可写。此处**只认数据根**：早先写法带一个 `or "hiveweave" in name`
    的兜底分支，使系统 temp 回退也能通过 ⇒ 断言不可证伪（审计 2026-09-14
    第 3 条）。该回退已删除，断言随之收紧。
    """
    from hiveweave.config import get_data_root

    hooks = Path(dict(ws.git_hardening_pairs())["core.hooksPath"]).resolve()
    data_root = Path(get_data_root()).resolve()
    assert hooks.is_relative_to(data_root), (
        f"空 hook 目录必须落在平台数据根内：hooks={hooks} data_root={data_root}"
    )
    parts = {p.lower() for p in hooks.parts}
    assert ".git" not in parts
    assert ".hiveweave" not in parts


def test_apply_git_hardening_is_reusable_across_hook_points() -> None:
    """跨挂钩点复用的公开入口：不就地改入参 + 追加语义 + 非非法值不炸。

    acl_sandbox 的 `_build_sandbox_env` 复用同一函数 ⇒ 两份实现不会漂移。
    """
    pairs = ws.git_hardening_pairs()
    caller = {"PATH": "x", "GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "core.pager"}
    out = ws.apply_git_hardening(caller)
    assert caller == {"PATH": "x", "GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_0": "core.pager"}
    assert out["GIT_CONFIG_COUNT"] == str(3 + len(pairs))
    assert out["GIT_CONFIG_KEY_0"] == "core.pager"
    assert out["GIT_CONFIG_KEY_3"] == pairs[0][0]
    for bad in ("abc", "-9", "", None):
        got = ws.apply_git_hardening({"GIT_CONFIG_COUNT": bad})
        assert got["GIT_CONFIG_COUNT"] == str(len(pairs)), bad

    # 幂等：同一 env 被接两层时 COUNT 不得翻倍
    once = ws.apply_git_hardening({"PATH": "x"})
    twice = ws.apply_git_hardening(once)
    assert twice["GIT_CONFIG_COUNT"] == once["GIT_CONFIG_COUNT"]
    assert twice == once


def test_shell_entry_is_hardened_unconditionally() -> None:
    """走 shell 的命令行跑不跑 git 不可预知 ⇒ 无条件加固。

    覆盖两种 shell 形态：`hidden_shell`（`always=True`）与
    `hidden_run/hidden_popen(..., shell=True)`（process_registry 的
    dev server spawn 走这条）。
    """
    for cmd in ("echo hi", "npm run build && echo ok"):
        kw = ws._with_git_hardening((cmd,), {}, always=True)
        assert "GIT_CONFIG_COUNT" in kw["env"], cmd
    kw = ws._with_git_hardening(("npm run dev",), {"shell": True})
    assert "GIT_CONFIG_COUNT" in kw["env"]
    # 非 shell、非 git 的 argv 形态：零扰动
    assert "env" not in ws._with_git_hardening(("npm", "run", "dev"), {})


def test_count_is_appended_not_claimed() -> None:
    """GIT_CONFIG_COUNT 是槽位总数语义 —— 必须追加，独占会挤掉调用方的键。"""
    pairs = ws.git_hardening_pairs()

    # (a) 无 env：基于 os.environ
    kw = ws._with_git_hardening(("git", "status"), {})
    env = kw["env"]
    assert env["GIT_CONFIG_COUNT"] == str(len(pairs))
    assert env["GIT_CONFIG_KEY_0"] == pairs[0][0]

    # (b) 调用方已有 2 个槽：必须保留 KEY_0/1 并追加
    caller = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.pager",
        "GIT_CONFIG_VALUE_0": "cat",
        "GIT_CONFIG_KEY_1": "user.name",
        "GIT_CONFIG_VALUE_1": "caller",
    }
    kw = ws._with_git_hardening(("git", "status"), {"env": caller})
    env = kw["env"]
    assert env["GIT_CONFIG_KEY_0"] == "core.pager"
    assert env["GIT_CONFIG_VALUE_1"] == "caller"
    assert env["GIT_CONFIG_COUNT"] == str(2 + len(pairs))
    assert env["GIT_CONFIG_KEY_2"] == pairs[0][0]
    assert caller["GIT_CONFIG_COUNT"] == "2", "不得就地修改调用方 dict"

    # (c) 非法/负 COUNT 不得炸（归 0 重写）
    for bad in ("abc", "-3", "", None):
        kw = ws._with_git_hardening(("git", "status"), {"env": {"GIT_CONFIG_COUNT": bad}})
        assert kw["env"]["GIT_CONFIG_COUNT"] == str(len(pairs)), bad

    # (d) 非 git 命令：kwargs 完全不动（零扰动）
    sentinel = {"env": {"PATH": "x"}}
    assert ws._with_git_hardening(("python", "-c", "1"), sentinel) is sentinel


# ══════════════════════════════════════════════════════════════════════
# 2. 穿透层（真子进程，无 mock）
# ══════════════════════════════════════════════════════════════════════
def test_hookspath_attack_is_blocked_through_funnel(repo: Path, tmp_path: Path) -> None:
    """core.hooksPath + post-commit + 平台跑 commit —— 唯一进过 §一 的路径。"""
    flag = tmp_path / "flag_hook.txt"
    hookdir = tmp_path / "evil-hooks"
    hookdir.mkdir()
    (hookdir / "post-commit").write_text(
        f"#!/bin/sh\necho hit > '{_fwd(flag)}'\n", encoding="utf-8", newline="\n")
    # 攻击现场：agent 写 <project>/.git/config（它有 GRANT_MASK）
    _raw_git(repo, "config", "core.hooksPath", _fwd(hookdir))

    # 先证现场是活的：不经漏斗必须执行（= 攻击成立）
    _raw_git(repo, "commit", "--allow-empty", "-q", "-m", "baseline")
    assert _hit(flag, reset=True), "阳性对照失败：攻击现场未生效，后续断言无意义"

    # 经漏斗（= 平台的真实路径）：必须被拦
    out = _funnel_git(repo, "commit", "--allow-empty", "-q", "-m", "via-funnel")
    assert not _hit(flag), (
        f"hooksPath 攻击穿透了漏斗加固！effective hooksPath="
        f"{_funnel_git(repo, 'config', '--get', 'core.hooksPath').stdout.strip()!r}\n"
        f"stdout={out.stdout!r} stderr={out.stderr!r}"
    )


def test_fsmonitor_attack_is_blocked_through_funnel(repo: Path, tmp_path: Path) -> None:
    """core.fsmonitor → git status（CVE-2026-19590 同族）。"""
    flag = tmp_path / "flag_fsm.txt"
    payload = _sh_payload(flag)
    _raw_git(repo, "config", "core.fsmonitor", _fwd(payload))

    _raw_git(repo, "status", "--porcelain")
    assert _hit(flag, reset=True), "阳性对照失败"

    _funnel_git(repo, "status", "--porcelain")
    assert not _hit(flag), "fsmonitor 攻击穿透了漏斗加固"


def test_platform_git_diff_content_is_not_broken(repo: Path) -> None:
    """★ #23 验收（状态判据）：**产出内容的 `git diff` 必须能跑出 `@@`**。

    这是取代旧用例 `test_diff_external_attack_is_blocked_through_funnel` 的
    **强化**形态（不是删除）：旧用例断言的"env 覆盖挡住攻击"在 #23 之后
    已**不可能**成立（`diff.external` 不再注入），而它保护的威胁模型
    （agent 写 `.git/config`）已由 #2 批⑤「双阶段锁死」封住。留下"攻击被挡"
    的断言只会掩盖真实状态。新断言对着**用户可见的功能**：
    平台侧（`anchored _git`，`code_audit` 就走这条）与 agent 侧都必须拿到 diff。

    ⚠ 曾经的实测代价（git 2.55.0）：`diff.external=""` ⇒ `error: cannot spawn :`
    + `fatal: external diff died`（rc=128）；而 `code_audit.py:515` 是
    `if ok and out:` ⇒ **diff 被静默丢掉**、审计看不到改动却不报错。
    """
    import asyncio

    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    _raw_git(repo, "add", "-A")
    _raw_git(repo, "commit", "-q", "-m", "second")

    # ① 经漏斗（= agent 与平台的真实路径）
    out = _funnel_git(repo, "diff", "HEAD~1...HEAD")
    assert out.returncode == 0, (out.returncode, out.stdout, out.stderr)
    assert "@@" in out.stdout, (out.returncode, out.stdout, out.stderr)

    # ② 经平台 anchored `_git`（code_audit / attestation 的真实入口）
    from hiveweave.services.git_worktree import _git

    ok, diff_out = asyncio.run(
        _git(["diff", "HEAD~1...HEAD"], str(repo), project_root=str(repo))
    )
    assert ok is True, diff_out
    assert "@@" in diff_out, diff_out

    # ③ 未提交改动（`git diff HEAD`）同样必须可用 —— code_audit 的第二条 diff
    (repo / "a.txt").write_text("uncommitted\n", encoding="utf-8")
    ok2, diff2 = asyncio.run(
        _git(["diff", "HEAD"], str(repo), project_root=str(repo))
    )
    assert ok2 is True, diff2
    assert "@@" in diff2, diff2


def test_hardening_still_blocks_a_planted_key_in_a_readable_config(repo, tmp_path):
    """★ 移交后仍要证明「可读的 config 里的键会被压过」—— 用**另一个仍在注入的**键。

    为什么必须留这一条：`diff.external` 退出注入清单后，本文件里"env 能压过
    config"这条机制就没有任何穿透层用例了。用 `core.hooksPath`（仍在注入）
    证明机制本身活着，避免"键没了 ⇒ 机制也没了"的静默退化。
    """
    flag = tmp_path / "flag_hook2.txt"
    hookdir = tmp_path / "evil-hooks2"
    hookdir.mkdir()
    (hookdir / "post-commit").write_text(
        f"#!/bin/sh\necho hit > '{_fwd(flag)}'\n", encoding="utf-8", newline="\n")
    _raw_git(repo, "config", "core.hooksPath", _fwd(hookdir))

    _raw_git(repo, "commit", "--allow-empty", "-q", "-m", "baseline")
    assert _hit(flag, reset=True), "阳性对照失败：攻击现场未生效，后续断言无意义"

    _funnel_git(repo, "commit", "--allow-empty", "-q", "-m", "via-funnel")
    assert not _hit(flag), "hooksPath 攻击穿透了漏斗加固"


def test_attr_tree_attack_is_blocked_through_funnel(repo: Path, tmp_path: Path) -> None:
    """attr.tree 让 .gitattributes 从任意 tree 读（报告标注未验证，本仓已实测成立）。"""
    flag = tmp_path / "flag_attr.txt"
    payload = _sh_payload(flag)
    _raw_git(repo, "config", "filter.evil.clean", _fwd(payload))
    (repo / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8", newline="\n")
    _raw_git(repo, "add", "-A")
    _raw_git(repo, "commit", "-q", "-m", "attrs")
    tree = _raw_git(repo, "write-tree").stdout.strip()
    _raw_git(repo, "config", "attr.tree", tree)
    os.chmod(repo / ".gitattributes", 0o600)
    (repo / ".gitattributes").unlink()

    (repo / "z1.txt").write_text("z\n", encoding="utf-8")
    _raw_git(repo, "add", "z1.txt")
    assert _hit(flag, reset=True), "阳性对照失败：attr.tree 现场未生效"

    (repo / "z2.txt").write_text("z\n", encoding="utf-8")
    _funnel_git(repo, "add", "z2.txt")
    assert not _hit(flag), "attr.tree 攻击穿透了漏斗加固"


def test_hardening_survives_whitelist_env(repo: Path, tmp_path: Path) -> None:
    """**P1-1 判据**：bash/pwsh 工具传白名单 env（整段替换）时加固仍生效。

    这是最容易被写错的一环 —— 只改 os.environ 的实现会在这里静默失守。
    """
    flag = tmp_path / "flag_env.txt"
    hookdir = tmp_path / "evil-hooks2"
    hookdir.mkdir()
    (hookdir / "post-commit").write_text(
        f"#!/bin/sh\necho hit > '{_fwd(flag)}'\n", encoding="utf-8", newline="\n")
    _raw_git(repo, "config", "core.hooksPath", _fwd(hookdir))

    _funnel_git(repo, "commit", "--allow-empty", "-q", "-m", "whitelist",
                env=_whitelist_env())
    assert not _hit(flag), "白名单 env 下加固失效 —— 加固没合并进 kwargs['env']"


def test_hardening_propagates_through_shell_child(repo: Path, tmp_path: Path) -> None:
    """P1-1 第二判据：加固 env 经 shell 孙进程（pwsh）继承后 git 仍被覆盖。"""
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("no pwsh/powershell on PATH")

    flag = tmp_path / "flag_shell.txt"
    hookdir = tmp_path / "evil-hooks3"
    hookdir.mkdir()
    (hookdir / "post-commit").write_text(
        f"#!/bin/sh\necho hit > '{_fwd(flag)}'\n", encoding="utf-8", newline="\n")
    _raw_git(repo, "config", "core.hooksPath", _fwd(hookdir))

    # 先证现场活着：裸 shell 里 git commit 必须执行 hook
    raw = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command",
         "git commit --allow-empty -q -m raw"],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=_whitelist_env(), timeout=120,
    )
    assert _hit(flag, reset=True), (
        f"阳性对照失败：裸 shell 未触发 hook（shell 是否可用于测试？）"
        f" rc={raw.returncode} err={raw.stderr[:300]!r}"
    )

    # 经漏斗 spawn shell（= tools/bash.py 的真实形态）
    proc = ws.hidden_popen(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command",
         "git commit --allow-empty -q -m funnel"],
        cwd=str(repo), env=_whitelist_env(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    proc.communicate(timeout=120)
    assert not _hit(flag), "加固 env 未传播到 shell 孙进程（P1-1 失效）"


def test_sandbox_env_is_hardened(repo: Path, tmp_path: Path) -> None:
    """**P1-1 沙箱路径判据**：agent 受限命令的 env 必须带加固键。

    agent 的 bash/pwsh 在沙箱开启时走 `spawn_confined` → `ConfinedRunner` 的
    `CreateProcessAsUserW`，**完全绕过 win_subprocess 漏斗**（故
    `test_spawn_funnel_guard.py` 扫不到这条路）。加固必须在
    `_build_sandbox_env`（唯一 env 点）补注入，否则该路静默失守 ——
    这是本仓踩过的「修了没生效」形态。
    """
    from hiveweave.services.acl_sandbox.service import _build_sandbox_env

    work = tmp_path / "ws"
    work.mkdir()
    for sub in ("cache", "temp"):
        (tmp_path / sub).mkdir()

    sandbox_env = _build_sandbox_env(
        str(work), str(tmp_path / "cache"), str(tmp_path / "temp"))

    assert "GIT_CONFIG_COUNT" in sandbox_env, "沙箱 env 未接加固（P1-1 会静默失效）"
    got = {
        sandbox_env[f"GIT_CONFIG_KEY_{i}"]: sandbox_env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(sandbox_env["GIT_CONFIG_COUNT"]))
    }
    for key, val in dict(ws.git_hardening_pairs()).items():
        assert got.get(key) == val, f"沙箱 env 缺/错加固键：{key}={got.get(key)!r}"

    # 端到端：拿这套沙箱 env 真跑 git commit，hook 必须没被执行
    flag = tmp_path / "flag_sbx.txt"
    hookdir = tmp_path / "evil-hooks-sbx"
    hookdir.mkdir()
    (hookdir / "post-commit").write_text(
        f"#!/bin/sh\necho hit > '{_fwd(flag)}'\n", encoding="utf-8", newline="\n")
    _raw_git(repo, "config", "core.hooksPath", _fwd(hookdir))
    _raw_git(repo, "commit", "--allow-empty", "-q", "-m", "raw")
    assert _hit(flag, reset=True), "阳性对照失败"

    ws.hidden_run(
        ["git", "commit", "--allow-empty", "-q", "-m", "sbx"],
        cwd=str(repo), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=sandbox_env, timeout=60,
    )
    assert not _hit(flag), "沙箱 env 里的加固键没压住 hook"


# ══════════════════════════════════════════════════════════════════════
# 3. 阳性对照（证明上面那些 BLOCKED 是加固带来的）
# ══════════════════════════════════════════════════════════════════════
def test_positive_control_empty_pairs_must_execute(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把加固清单清空 → 同一构造必须转红。

    没有这条，"BLOCKED" 可能只是探针盲区 / 环境巧合。
    """
    monkeypatch.setattr(ws, "_HARDENING_PAIRS", [])
    flag = tmp_path / "flag_pc.txt"
    hookdir = tmp_path / "evil-hooks-pc"
    hookdir.mkdir()
    (hookdir / "post-commit").write_text(
        f"#!/bin/sh\necho hit > '{_fwd(flag)}'\n", encoding="utf-8", newline="\n")
    _raw_git(repo, "config", "core.hooksPath", _fwd(hookdir))

    _funnel_git(repo, "commit", "--allow-empty", "-q", "-m", "pc")
    assert _hit(flag), (
        "阳性对照失败：清空加固后攻击仍未执行 ⇒ 说明被测行为不是加固造成的，"
        "上面的 BLOCKED 断言不可信"
    )


def test_positive_control_hooks_path_override_is_what_blocks(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只保留 fsmonitor、去掉 hooksPath → hooksPath 攻击必须转红。

    证明 hooksPath 这一键确实是拦点，而不是被别的键顺手挡住。
    """
    pairs = [p for p in ws.git_hardening_pairs() if p[0] != "core.hooksPath"]
    monkeypatch.setattr(ws, "_HARDENING_PAIRS", pairs)
    flag = tmp_path / "flag_pc2.txt"
    hookdir = tmp_path / "evil-hooks-pc2"
    hookdir.mkdir()
    (hookdir / "post-commit").write_text(
        f"#!/bin/sh\necho hit > '{_fwd(flag)}'\n", encoding="utf-8", newline="\n")
    _raw_git(repo, "config", "core.hooksPath", _fwd(hookdir))

    _funnel_git(repo, "commit", "--allow-empty", "-q", "-m", "pc2")
    assert _hit(flag), "去掉 hooksPath 覆盖后攻击仍被拦 ⇒ 该键的作用未被验证"


# ══════════════════════════════════════════════════════════════════════
# 4. 已知缺口登记（strict xfail）——**env 层**的固有残余，不是 #2 的进度信号
# ══════════════════════════════════════════════════════════════════════
@pytest.mark.xfail(
    strict=True,
    reason=(
        "**env 层**的固有残余（报告边界 ①）：filter.<name>.clean / merge.<name>.driver "
        "的 <name> 由**仓库内 .gitattributes 动态指定**，静态键名清单结构上列不出来，"
        "所以 `GIT_CONFIG_*` 加固永远覆盖不到这一族。实测排除两条替代方案："
        "core.attributesFile 指向空文件挡不住（第一轮 4c 仍 EXECUTED）；也试不出可注入"
        "的通配键。"
        ""
        "⚠ **本条不是 #2 的进度信号**（计划原先写「P2 落地后它会转红」—— 那句错了）："
        "本用例的攻击现场是 harness 用 `_raw_git`（**不受限**）写出来的 config，"
        "ACL 收窄（#2）改变的是「受限 agent 能不能写 config」，不会让这条断言转红。"
        "strict=True 在这里的语义 = 「env 层这条缺口仍在」，它会**长期红着**。"
        ""
        "#2 的进度信号在 tests/test_git_config_seal.py："
        "① test_agent_cannot_replace_git_config（受控 agent 写不进 config = 载体死）；"
        "② 两条 xfail（test_agent_cannot_delete_git_config / "
        "test_agent_cannot_write_worktree_gitdir_carrier）转红 = 残余被修掉。"
    ),
)
def test_dynamic_key_paths_are_known_gap_until_p2(repo: Path, tmp_path: Path) -> None:
    """如实记录「env 层」挡不住的动态键名路径（filter/merge driver 同族）。

    ⚠ 这条**不是**「期望攻击成功」，而是「已知缺口登记」：断言写的是正常期望
    （应当被拦住），当前拦不住 ⇒ xfail。它的前提（config 被写入）由 harness 直接
    制造，与 agent 有无写权限无关 ⇒ **它长期为红**，见 xfail reason。
    """
    flag = tmp_path / "flag_gap.txt"
    payload = _sh_payload(flag)
    _raw_git(repo, "config", "filter.evil.clean", _fwd(payload))
    (repo / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8", newline="\n")
    (repo / "g1.txt").write_text("g\n", encoding="utf-8")

    # 阳性对照：现场必须活着，否则这条 xfail 是探针盲区而非真缺口
    _raw_git(repo, "add", "g1.txt")
    assert _hit(flag, reset=True), "阳性对照失败：动态键名现场未生效"

    (repo / "g2.txt").write_text("g\n", encoding="utf-8")
    _funnel_git(repo, "add", "g2.txt")
    assert not _hit(flag), (
        "动态键名路径穿透了 P0 加固 —— 这是当前**预期行为**（见 xfail reason）；"
        "若此断言通过，说明缺口已被 P2 补上，请把本测试改成正常用例"
    )


# ══════════════════════════════════════════════════════════════════════
# 5. 收口层：报告列的 8 个 `_git()` 绕过点必须被判为「需加固」
# ══════════════════════════════════════════════════════════════════════
def test_known_git_callsite_shapes_are_all_hardened() -> None:
    """8 个绕过 _git() 的裸调用点，其真实 argv 形态必须全部命中加固。

    取证：apps/hiveweave-py/src/hiveweave/ 下各文件的实际写法（2026-09-14 快照）。
    这些点全部经 hidden_* 漏斗 ⇒ 加固自动覆盖，无需逐点改 _git()（改了反而
    要处理 bool-返回/退出码/sync-async 三种不同签名）。
    """
    shapes = {
        "main.py:248": (["git", "-C", "ws", "stash", "list"],),
        "dispatch_facts.py:30": (["git", "rev-parse", "HEAD"],),
        "conflict_predict.py:83": ("git", "merge-tree", "--write-tree", "b", "r"),
        "bash.py:431": (["git", "rev-parse", "--short", "HEAD"],),
        "bash.py:2953": ("git", "rev-parse", "HEAD"),
        "browse_tools.py:183": ("git", "rev-parse", "HEAD"),
        "dev_server_tools.py:400": (["git", "rev-parse", "--short", "HEAD"],),
        "host_env/probes/toolchain.py:33": ("git", "--version"),
    }
    for where, shape in shapes.items():
        assert ws._argv_needs_git_hardening(shape) is True, (
            f"{where} 的 git 调用形态未被判为需加固：{shape!r}"
        )


def test_callsite_snapshot_files_still_contain_git_calls() -> None:
    """防快照腐化：上面那份 argv 清单是**手抄快照**（审计 2026-09-14 第 4 条）。

    这些文件一旦改名/删除/把调用重构成别的形态，手抄清单就会静默失真。
    本测试只保证「快照对应的文件仍在且仍含 git 调用」，转红即提醒重新核对。
    """
    for rel in (
        "main.py",
        "services/dispatch_facts.py",
        "services/git_worktree/conflict_predict.py",
        "tools/bash.py",
        "tools/browse_tools.py",
        "tools/dev_server_tools.py",
        "services/host_env/probes/toolchain.py",
    ):
        path = SRC_DIR / rel
        assert path.exists(), f"快照文件不存在了，请更新上面的 argv 清单：{rel}"
        src = path.read_text(encoding="utf-8", errors="replace")
        assert '"git"' in src or "'git'" in src, (
            f"{rel} 里已找不到 git 调用字面量 —— 快照可能已腐化，请核对后更新"
        )


def test_git_cmd_goes_through_the_funnel() -> None:
    """正统入口 git_worktree/git_cmd.py::_git 必须经漏斗（否则整层加固绕过）。"""
    src = (SRC_DIR / "services" / "git_worktree" / "git_cmd.py").read_text(
        encoding="utf-8")
    assert "win_subprocess import hidden_exec" in src
    assert "hidden_exec(" in src
