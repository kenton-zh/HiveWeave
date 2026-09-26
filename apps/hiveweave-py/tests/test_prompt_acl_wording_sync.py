"""提示词 ACL 口径统一绊线（批 F 第 4 步，2026-09-26 审计 P1·L3 / P2-17）。

背景：`prompts/identity.py` 曾把 `.hiveweave/reports/` 笼统列为
"Work files (ALLOWED)"，而 ACL 真相是 reports/（及 drafts/）**没有 shell 能力
授权** —— shell/pwsh 写必被拒（`services/acl_sandbox/service.py` 的
no_write_sid 提示即为此成因），唯一写通道是平台文件工具
（write_file / edit_file / apply_patch）；全员 shell 可写的团队交换子树只有
`.hiveweave/shared/`（能力 ACE 全边界授予，`policy.py build_write_sids`）。
提示词自相矛盾会把 agent 引向「按提示用 shell 写 reports → 被拒 → 换写法
重试 → 再被拒」的撞墙循环。

本文件把「同一事实、一套口径」钉成绊线：

1. identity 渲染产物里 Work files 段必须带 write_file 通道 + ACL 拒 shell 限定；
   不再允许无限定的「Work files (ALLOWED)」授予。
2. shared/ 段必须写明它是 shell/pwsh 可写的团队交换通道。
3. 跨树只读窄门（read_file 的 tree= 参数）必须出现在 identity 里。
4. prompts/ 全目录源码：不得残留「CEO 无 SOURCE_WRITE / CEO 无 bash」式旧句
   （批 A 后 CEO 有 SOURCE_WRITE/BASH_SHELL，见 coordinator.py 行政边界段）；
   不得出现「shell 可写 reports/drafts」式暗示。「ALLOWED 无限定授予」与
   「跨树读不到/无法访问」两条正则按审计 P2-4 **收窄到语境段**（Work files 段 /
   identity worktrees 段）：这两类词在其他语境（合法的限定授予、系统目录
   保护声明）是正确句，全目录扫描会误绊 —— 各测试 docstring 写明理由。
"""

from __future__ import annotations

import re
from pathlib import Path

from hiveweave.prompts.identity import build_identity_prompt

_SRC = Path(__file__).resolve().parents[1] / "src" / "hiveweave"
_PROMPTS = _SRC / "prompts"


def _prompt_files() -> list[Path]:
    return sorted(p for p in _PROMPTS.glob("*.py") if p.name != "__init__.py")


def _prompts_text() -> str:
    return "\n".join(f.read_text(encoding="utf-8") for f in _prompt_files())


def _bullets(prompt: str) -> list[str]:
    """按「\\n- 」切 bullet（_SYSTEM_DIR_BLOCK 的条目都是 "- **xxx**:" 起头）。"""
    return re.split(r"\n- ", prompt)


def _identity_prompt(role: str, role_type: str) -> str:
    return build_identity_prompt(role, role_type, "")


# ── 1. Work files 段：write_file 通道 + ACL 拒 shell ─────────────


def test_work_files_bullet_names_write_file_channel_and_acl_rejection():
    for role_type in ("executor", "coordinator"):
        prompt = _identity_prompt("developer", role_type)
        work = [b for b in _bullets(prompt) if b.startswith("**Work files")]
        assert len(work) == 1, f"[{role_type}] Work files 段缺失或多于一条"
        bullet = work[0]
        # reports/ 与 drafts/ 同待遇（都没有 shell 能力授权），同句限定
        assert ".hiveweave/reports/" in bullet and ".hiveweave/drafts/" in bullet
        assert "write_file" in bullet, "Work files 段必须点名 write_file 通道"
        assert "ACL" in bullet, "Work files 段必须写明 shell 写会被 ACL 拒"
        assert "shell" in bullet, "Work files 段必须点名 shell/pwsh 写会被拒"


def test_no_unqualified_allowed_grant_on_reports():
    """旧病灶原文不得回归；Work files 段不得出现无限定的 ALLOWED/允许 授予。

    断言范围收窄理由（独立审计 P2-4）：ALLOWED 正则**不扫全目录** ——
    shared 段的「(ALLOWED, read+write)」与 worktrees 段的
    「(ALLOWED to owners / mid-level review)」都是带限定的合法授予，
    未来还可能出现别的正确 ALLOWED 句（如系统目录保护语境），全目录扫描
    会误伤。语境化断言只盯 Work files 段（reports/drafts 授予唯一所在）；
    旧病灶原文精确串是零误报面的全局钉扎，保留全目录。
    """
    # 旧病灶原文精确串：串本身即病灶定义，全局扫无误报面
    assert "Work files (ALLOWED)" not in _prompts_text()
    for role_type in ("executor", "coordinator"):
        prompt = _identity_prompt("developer", role_type)
        work = [b for b in _bullets(prompt) if b.startswith("**Work files")]
        assert len(work) == 1, f"[{role_type}] Work files 段缺失或多于一条"
        assert not re.search(r"ALLOWED|允许", work[0]), (
            f"[{role_type}] Work files 段出现无限定的 ALLOWED/允许 授予 —— "
            "reports/drafts 只放行平台文件工具写（write_file / edit_file / "
            "apply_patch），shell/pwsh 写被 ACL 拒；授予表述必须带这个限定。"
        )


# ── 2. shared/ 段：shell 可写的团队交换通道 ──────────────────────


def test_shared_bullet_names_shell_writable_team_exchange_channel():
    for role_type in ("executor", "coordinator"):
        prompt = _identity_prompt("developer", role_type)
        shared = [b for b in _bullets(prompt) if b.startswith("**Team shared space")]
        assert len(shared) == 1, f"[{role_type}] shared 段缺失或多于一条"
        bullet = shared[0]
        assert ".hiveweave/shared/" in bullet
        assert "shell" in bullet, (
            "shared 段必须写明 shell/pwsh 也能写（唯一全员放行的团队交换子树）——"
            "这是它与 reports/drafts 的分界"
        )


# ── 3. tree= 跨树只读窄门 ────────────────────────────────────────


def test_identity_mentions_tree_param_narrow_gate():
    prompt = _identity_prompt("developer", "executor")
    assert "tree=" in prompt, (
        "identity 必须提到 read_file 的 tree= 跨树只读窄门"
        "（与 util/path_guard.py OUT_OF_BOUNDARY_HINT 同一口径）"
    )
    assert "越界" in prompt, "必须同时说明直接拼对方树路径会被越界拒绝"
    # P2-1 钉扎：越界拒绝只对 worktree 边界角色成立（path_guard.py
    # is_foreign_worktree_ref：项目根角色指向任意 worktree 读侧合法）——
    # 括注必须带「worktree 内」限定，防止 CEO/HR 被误导。
    wt = [b for b in _bullets(prompt) if b.startswith("**Implementation worktrees")]
    assert len(wt) == 1, "identity worktrees 段缺失或多于一条"
    assert "worktree 内" in wt[0], "越界拒绝的括注必须带「worktree 内」作用域限定"


# ── 4. 全目录源码绊线 ────────────────────────────────────────────


def test_no_ceo_permission_remnants_in_prompts():
    """批 A 后 CEO 有 SOURCE_WRITE/BASH_SHELL —— 旧句「CEO 无 bash/无 SOURCE_WRITE」必须清零。"""
    pattern = re.compile(
        r"(CEO|首席执行官)[^\n]{0,80}"
        r"(无|没有|不能|无法|不可|cannot|can't|does\s*not\s*have|has\s*no)[^\n]{0,40}"
        r"(SOURCE_WRITE|BASH_SHELL|bash|pwsh)",
        re.IGNORECASE,
    )
    offenders = []
    for f in _prompt_files():
        src = f.read_text(encoding="utf-8")
        for m in pattern.finditer(src):
            line_no = src[: m.start()].count("\n") + 1
            offenders.append(f"{f.name}:{line_no} {m.group(0)[:80]!r}")
    assert not offenders, (
        "prompts 里残留「CEO 无 SOURCE_WRITE/bash」式旧句（批 A 已给 CEO "
        "SOURCE_WRITE/BASH_SHELL，coordinator.py 行政边界段是权威口径）：\n  "
        + "\n  ".join(offenders)
    )


def test_no_shell_write_hint_for_reports_or_drafts():
    """不得出现「reports/drafts 可用 shell/pwsh 写」式暗示（会诱导撞 ACL 墙）。"""
    pattern = re.compile(
        r"(reports|drafts)[^\n]{0,60}"
        r"(shell|pwsh|Copy-Item|Set-Content|Out-File|bash)[^\n]{0,40}"
        r"(可写|可以写|能写|writable|ALLOWED)",
        re.IGNORECASE,
    )
    offenders = []
    for f in _prompt_files():
        src = f.read_text(encoding="utf-8")
        for m in pattern.finditer(src):
            line_no = src[: m.start()].count("\n") + 1
            offenders.append(f"{f.name}:{line_no} {m.group(0)[:80]!r}")
    assert not offenders, (
        "prompts 里出现「shell 可写 reports/drafts」式暗示：\n  "
        + "\n  ".join(offenders)
        + "\n→ 正确口径：reports/drafts 只放行平台文件工具（write_file 等），"
        "shell/pwsh 写被 ACL 拒；全员 shell 可写的团队子树是 .hiveweave/shared/。"
    )


def test_no_absolutist_crosstree_unreadable_claims():
    """跨树口径所在段不得出现「读不到/无法访问」绝对化表述（read_file 已有 tree= 窄门）。

    断言范围收窄理由（独立审计 P2-4）：绝对化词正则**不扫全目录** ——
    「无法访问/读不到」在**系统目录保护**语境是正确句（如「tool_outputs
    无法访问」「data.db 读不到 = 平台自管」），全目录扫描会把未来的正当
    保护句误绊成失败。语境化断言只盯跨树口径实际所在的 identity
    worktrees 段（tree= 窄门句所在处）。
    """
    prompt = _identity_prompt("developer", "executor")
    wt = [b for b in _bullets(prompt) if b.startswith("**Implementation worktrees")]
    assert len(wt) == 1, "identity worktrees 段缺失或多于一条"
    assert not re.search(
        r"读不到|无法读|读不了|无法访问|cannot\s+read|inaccessible",
        wt[0],
        re.IGNORECASE,
    ), (
        "worktrees 段出现跨树「读不到/无法访问」绝对化表述 → 正确口径："
        "只读取物用 read_file(filePath=..., tree=<目标树 id>)；worktree 内"
        "直接拼对方树路径会被越界拒绝（见 OUT_OF_BOUNDARY_HINT）。"
    )
