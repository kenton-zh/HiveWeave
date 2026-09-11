"""#6 路径护栏：越出授权树 + 幽灵树 + spawn 前命令串预检。

判据出处（**我们自己的模型**，fixplan §10.3 —— 此处**不引 DSH**）：
- ``services/acl_sandbox/policy.py:54`` —— ``boundary_root``
  「授权树根（executor=worktree / 项目根角色=项目根）」⇒「命令里出现
  ``.hiveweave/worktrees/<非本树 id>/`` = 效果落点越出授权树」，
  这正是 boundary_root 已在判的事，**不是新规则**；
- ``services/git_worktree/service_create.py:99-105`` 四目录跨树共享
  + ``dispatch_pin.py:7,34`` worktree 落点 + ``constants.py:9`` QUARANTINE。

⚠️ 明确不做的：DSH 式「bash 与 fs 共用同一 ``FsSandboxController``」——
那个 controller 管**单棵树内**的路径、**没有"是不是本 agent 的树"这个概念**，
照它改会做出**在多 worktree 下错误**的统一 sandbox（fixplan §0.5/§10.3 已废弃）。

断言必须能被**代码回退打红**（不看 docstring）。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hiveweave.tools.bash import precheck_command_string, with_cwd_display
from hiveweave.util import path_guard


@pytest.fixture
def trees(tmp_path: Path) -> dict[str, str]:
    project = tmp_path / "project"
    wt_a = project / ".hiveweave" / "worktrees" / "A044"
    wt_b = project / ".hiveweave" / "worktrees" / "A045"
    wt_a.mkdir(parents=True)
    wt_b.mkdir(parents=True)
    return {"project": str(project), "wt_a": str(wt_a), "wt_b": str(wt_b)}


# ── ① 越出授权树：shell 侧与 file 侧**同一判定** ────────────────────


def test_foreign_worktree_ref_only_for_own_tree(trees: dict[str, str]) -> None:
    """本树引用 = 合法；别的树 = 越界（判据边界 = boundary_root）。"""
    assert path_guard.is_foreign_worktree_ref(
        trees["wt_a"] + "/src/x.py", trees["wt_a"]
    ) is False
    assert path_guard.is_foreign_worktree_ref(
        trees["wt_b"] + "/src/x.py", trees["wt_a"]
    ) is True


def test_project_root_role_may_reference_any_worktree(trees: dict[str, str]) -> None:
    """项目根角色（CEO/HR）的授权树 = 项目根 ⇒ 指向任意 worktree 是**读侧审查**
    的合法形态，不得误判成越界（否则会砍掉中层 review）。"""
    assert path_guard.is_foreign_worktree_ref(
        trees["wt_b"] + "/src/x.py", trees["project"]
    ) is False


def test_non_worktree_path_never_flagged_as_cross_tree(trees: dict[str, str]) -> None:
    """普通项目文件不是"跨树引用"这一维的事（写隔离另有机制）。"""
    assert path_guard.is_foreign_worktree_ref("src/app.py", trees["wt_a"]) is False
    assert path_guard.is_foreign_worktree_ref("", trees["wt_a"]) is False


# ── ①b P1-2：平台自管 `_quarantine` 不得被当"别的树" ────────────────
#
# 审计实测：`is_foreign_worktree_ref` 不豁免平台自管的 `_quarantine`
# （`constants.py:9`）⇒ 该目录是**平台自己**搬迁隔离用的，不属任何 agent，
# 却被 shell 预检判成"越出授权树"拒发。file/vision 侧早已跳过 `_` 前缀
# （`file.py:548` / `vision.py:204`），shell 侧此前没有 —— 三处口径不一。


def test_quarantine_dir_is_not_a_foreign_worktree(trees: dict[str, str]) -> None:
    """`_quarantine` 路径不得被判成跨树引用（平台自管，非任何 agent 的树）。"""
    q = trees["project"] + "/.hiveweave/worktrees/_quarantine/A044-20260912"
    assert path_guard.is_foreign_worktree_ref(q, trees["wt_a"]) is False


def test_other_underscore_prefixed_platform_dirs_exempt(trees: dict[str, str]) -> None:
    """任何 `_` 前缀 id 都是平台自管命名空间（与 file/vision 同一豁免口径）。"""
    p = trees["project"] + "/.hiveweave/worktrees/_tmp_purge/x"
    assert path_guard.is_foreign_worktree_ref(p, trees["wt_a"]) is False


def test_normal_sibling_tree_still_flagged_after_exemption(
    trees: dict[str, str],
) -> None:
    """豁免不得扩大到正常兄弟树（`A045` 仍必须判越界）—— 防放水。"""
    assert path_guard.is_foreign_worktree_ref(
        trees["wt_b"] + "/src/x.py", trees["wt_a"]
    ) is True


def test_precheck_allows_quarantine_path(trees: dict[str, str]) -> None:
    """shell 预检必须与豁免一致：`_quarantine` 命令不被拒发。"""
    q = trees["project"] + "/.hiveweave/worktrees/_quarantine/x/src"
    assert precheck_command_string(f"Remove-Item -Recurse -Force {q}",
                                  trees["wt_a"]) is None


# ── ② 幽灵树判定抽到 path_guard 后**行为不变**（file 侧是薄封装）───


def test_double_worktree_prefix_matches_file_side_helper(
    trees: dict[str, str],
) -> None:
    """抽取后 file 侧薄封装与 path_guard 的实现必须**逐字同结果**。"""
    from hiveweave.tools import file as file_mod

    wt_a = trees["wt_a"]
    cases = [
        wt_a + "/.hiveweave/worktrees/A044/x.py",   # 同 id 重复 → 幽灵
        wt_a + "/.hiveweave/worktrees/A045/x.py",   # 跨 id → 幽灵
        wt_a + "/src/x.py",                        # 正常 → 否
        wt_a + "/.hiveweave/tool_outputs/big.txt", # 平台自管目录 → 否
        wt_a,                                      # 自身 → 否
    ]
    for full in cases:
        assert (
            path_guard.double_worktree_prefix(wt_a, full)
            == file_mod._double_worktree_prefix(wt_a, full)
        ), full


def test_shared_hint_and_guard_are_language_consistent() -> None:
    """处方必须是中文可执行指路，且**不得**教模型手写别的树路径。"""
    hint = path_guard.OUT_OF_BOUNDARY_HINT
    assert "授权树根" in hint
    assert "boundary_root" in hint
    # 指路走读侧自动跨树，而不是"你自己去拼别的树路径"
    assert "自动跨树查找" in hint


# ── ③ spawn 前命令串预检：拒发 + 处方（不猜译、不改写）────────────


def test_precheck_blocks_foreign_tree_in_command(trees: dict[str, str]) -> None:
    reason = precheck_command_string(
        f"Remove-Item -Recurse -Force {trees['wt_b']}/src", trees["wt_a"]
    )
    assert reason is not None
    assert reason.startswith("Command blocked:")
    assert "授权树根" in reason


def test_precheck_allows_own_tree_and_normal_commands(trees: dict[str, str]) -> None:
    assert precheck_command_string("python -m pytest -q", trees["wt_a"]) is None
    assert precheck_command_string(
        f"rm -rf {trees['wt_a']}/src", trees["wt_a"]
    ) is None


def test_precheck_root_role_review_not_blocked(trees: dict[str, str]) -> None:
    """项目根角色的 review 命令（指向某棵 worktree）不得被预检拦下。"""
    assert precheck_command_string(
        f"Get-ChildItem {trees['wt_b']}/src", trees["project"]
    ) is None


def test_precheck_only_guards_boundary_not_unix_dialect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**分工守卫**：unix-only 不归本预检管（否则与封闭集翻译抢跑）。

    实测依据：``detect_untranslated_unix`` 是曾写在本处的
    ``_UNIX_ONLY_PRECHECK_RE`` 的**严格超集**（head/tail/grep/wc/sed/awk/
    xargs/find/uniq/… 全中），再叠一层是死代码 —— 且会复现已踩过的回归
    （`git log | head -3` 属封闭集，要**翻译**不要**拒绝**）。
    本用例钉住：预检对纯 unix 命令一律放行，交给既有方言门。
    """
    from hiveweave.tools import bash as bash_mod

    monkeypatch.setattr(bash_mod, "_pwsh_is_effective_shell", lambda: True)
    # 纯 unix（无越树）⇒ 预检必须放行
    assert precheck_command_string("git log | head -3", "") is None
    assert precheck_command_string("cat f | grep x | wc -l", "") is None
    # 而既有方言门**确实**拦它们（证明分工不是放水）
    assert bash_mod.detect_untranslated_unix("git log | head -3") is not None
    assert bash_mod.detect_untranslated_unix("cat f | grep x | wc -l") is not None


def test_dialect_gate_covers_pipe_tail_via_segment_split() -> None:
    """**管道尾覆盖守卫**：方言门靠 ``_split_command_segments`` 切管道，
    每段再查 head token ⇒ 管道尾的 unix 命令同样命中。

    ⚠️ 曾经的错误结论（已由 team-lead 审计推翻，留档警示）：我曾据 ``|``/``;``
    切段就断言 head-token 路**完全覆盖**管道尾，据此删掉了预检的 unix 分支 ——
    **错了**：``|``/``;``/``&&`` 会切，但**单 ``&``** 和 ``for…do`` / ``if…then``
    的**关键字 head** 都不覆盖（见 ``test_segmenter_splits_single_ampersand`` 与
    ``test_head_token_skips_shell_keywords``）。教训：探针形态单一 ⇒ 覆盖假象。

    本用例钉住 **``|`` 这条**确实成立（修分段器后仍须成立）。"""
    from hiveweave.tools import bash as bash_mod

    # 段确实被管道切开（机制前提）
    assert len(bash_mod._split_command_segments("git log | head -5")) == 2
    # 段首合法 + 管道尾 unix ⇒ 必须被拦
    for cmd in ("git log | head -5", "cat f | grep x", "ls | wc -l",
                "git log | tail -3", "cat f | sed -e s/a/b/",
                "echo hi && git log | head -5"):
        assert bash_mod.detect_untranslated_unix(cmd) is not None, cmd
    # 纯净命令不得误拦
    assert bash_mod.detect_untranslated_unix("git log --oneline -5") is None
    assert bash_mod.detect_untranslated_unix("python -m pytest -q") is None


def test_segmenter_splits_single_ampersand_not_fd_redirect() -> None:
    """**P1-1 根因守卫**：单 `&`（后台/PS 分隔符）必须切段，fd 重定向不能切。

    审计反例：``sleep 1 & tail -f log`` 此前整串一段、head=`sleep` ⇒ 管道尾的
    `tail` 永远漏网（agent 里极高频）。这是**切分器的缺陷**，修此处让所有
    segments 消费者（unix 检测 + 跨树预检）一并受益。

    回滚探针：把 ``_split_command_segments`` 的 `&` 分支删掉即转红。"""
    from hiveweave.tools import bash as bash_mod

    segs = bash_mod._split_command_segments("sleep 1 & tail -f log")
    assert len(segs) == 2, f"单 & 未切段：{segs}"
    assert bash_mod.detect_untranslated_unix("sleep 1 & tail -f log") is not None

    # 误杀检查：fd 重定向的 `&` **不能**切（`&&` 是逻辑与，本就该切成两段）
    for cmd in ("python -u x.py 2>&1", "cmd &> out.txt", "cmd 2>&1"):
        assert len(bash_mod._split_command_segments(cmd)) == 1, cmd
    assert len(bash_mod._split_command_segments("a && b")) == 2  # 既有语义不变
    for cmd in ("python -u x.py 2>&1 | Where-Object { $_ -match 'x' }",
                "cmd &> out.txt", "python -u x.py > out 2>&1"):
        assert bash_mod.detect_untranslated_unix(cmd) is None, cmd


def test_head_token_skips_shell_keywords() -> None:
    """**P1-1 根因守卫**：``for…do`` / ``if…then`` 切段后 head 是关键字，
    取 head 时必须跳过 ``do``/``then``/``fi``/``done`` 等。

    审计反例：``for i in 1 2; do head -1 f; done`` 三段 head 分别是
    ``for``/``do``/``done`` ⇒ 永远匹配不上 unix 词表。
    回滚探针：删掉 ``_segment_head_token`` 的关键字跳过循环即转红。"""
    from hiveweave.tools import bash as bash_mod

    cases = [
        "for i in 1 2; do head -1 f; done",
        "if true; then head -1 f; fi",
        "for i in 1; do sed -e s/a/b/ f; done",
        "while true; do grep x f; done",
    ]
    for cmd in cases:
        assert bash_mod.detect_untranslated_unix(cmd) is not None, cmd
    # 关键字本身不等于 unix 命令：纯 shell 循环不得误拦
    assert bash_mod.detect_untranslated_unix(
        "for i in 1 2; do echo hi; done") is None
    assert bash_mod.detect_untranslated_unix("if true; then echo hi; fi") is None


def test_head_token_skips_keywords_does_not_leak_into_next_word() -> None:
    """跳过关键字不得"吃掉"下一个词的首字符（`do` 后紧跟 `done` 等边界）。"""
    from hiveweave.tools import bash as bash_mod

    assert bash_mod._segment_head_token(" done") == ""
    assert bash_mod._segment_head_token(" do head -1 f") == "head"
    assert bash_mod._segment_head_token(" then sed x") == "sed"
    assert bash_mod._segment_head_token(" else grep x") == "grep"
    # 非关键字且以关键字为前缀的词不得被跳过
    assert bash_mod._segment_head_token(" donek") == "donek"
    assert bash_mod._segment_head_token(" headless") == "headless"


def test_cross_tree_precheck_survives_single_ampersand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Q2 结论守卫**：跨树引用预检扫**段内全 token**，本就不受单 `&` 不分段
    影响；修分段器后仍须成立（防两处根因耦合）。"""
    from hiveweave.tools import bash as bash_mod

    ws = os.path.join(os.sep, "proj", ".hiveweave", "worktrees", "A044")
    for cmd in ("sleep 1 & cat .hiveweave/worktrees/A045/x",
                "for i in 1 2; do cat .hiveweave/worktrees/A045/x; done",
                "if true; then rm .hiveweave/worktrees/A045/x; fi"):
        assert bash_mod.precheck_command_string(cmd, ws) is not None, cmd
    # 本树引用不得误拦
    own = f"cat {ws}/src/x.py"
    assert bash_mod.precheck_command_string(own, ws) is None


def test_dialect_gate_covers_find_and_former_precheck_words() -> None:
    """**词表守卫**：曾由预检正则兜的 unix 词条，方言门必须全数覆盖。

    其中 ``find`` 是**真实缺口**（原不在 ``_UNIX_ONLY_HINTS`` 里）：删除
    预检后若无本用例，`find` 会从"被拦"悄悄变成"放行"。
    回滚探针：从 ``_UNIX_ONLY_HINTS`` 删掉 ``find`` 即转红。"""
    from hiveweave.tools import bash as bash_mod

    assert bash_mod.detect_untranslated_unix("find . -name '*.py'") is not None
    assert bash_mod.detect_untranslated_unix("ls | find x") is not None
    words = ["head", "tail", "grep", "wc", "sed", "awk", "xargs", "find",
             "nl", "tr", "uniq", "cut", "basename", "dirname", "realpath",
             "readlink", "df", "du", "seq", "which", "env"]
    gaps = [w for w in words
            if bash_mod._UNIX_ONLY_HINTS.get(w) is None]
    assert gaps == [], f"方言门词表缺以下 unix 词条：{gaps}"


def test_closed_pipe_tail_translation_is_independent_of_precheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**分工不变量**：封闭集管道尾的翻译与越界预检**正交**。

    回归来源（实测踩过）：曾把 unix 位置无关预检塞进 spawn 前 →
    `git log --oneline -5 | head -3` 被拒而非翻译，
    ``tests/test_batch_a_gates.py::test_execute_bash_auto_translates_closed_pipe_tail``
    转红。现设计把 unix 维度交还方言门（在翻译**之后**），故本用例钉住：
    翻译仍发生，且越界预检不因 unix 形态而误触发。"""
    from hiveweave.tools import bash as bash_mod

    monkeypatch.setattr(bash_mod, "_pwsh_is_effective_shell", lambda: True)
    translated = bash_mod.try_closed_pipe_translation("git log --oneline -5 | head -3")
    assert translated is not None, "封闭集必须仍能翻译"
    new_cmd, _orig = translated
    assert "Select-Object -First 3" in new_cmd
    # 越界预检只管越界：纯 unix 命令（无越树）一律放行
    assert bash_mod.precheck_command_string(new_cmd, "") is None
    assert bash_mod.precheck_command_string(
        "git log --oneline -5 | head -3", ""
    ) is None


def test_precheck_boundary_is_dialect_independent(
    monkeypatch: pytest.MonkeyPatch, trees: dict[str, str],
) -> None:
    """越界判定与 shell 方言**正交** ⇒ native Git Bash 下同样拦。

    （unix-only 那一维才看方言；越界判的是"效果落点"，与解释器无关。）"""
    from hiveweave.tools import bash as bash_mod

    monkeypatch.setattr(bash_mod, "_pwsh_is_effective_shell", lambda: False)
    assert bash_mod.precheck_command_string(
        f"rm -rf {trees['wt_b']}/src", trees["wt_a"]
    ) is not None
    assert bash_mod.precheck_command_string("cat f | grep x", trees["wt_a"]) is None


def test_precheck_empty_command_is_noop() -> None:
    assert precheck_command_string("", "") is None
    assert precheck_command_string("   ", "") is None


@pytest.mark.asyncio
async def test_execute_bash_wires_the_boundary_precheck(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """**接线守卫**：``execute_bash`` 必须在 spawn 前调用 ``precheck_command_string``。

    只测纯函数是**不够**的（把 execute_bash 里那一段删掉，所有单元用例仍
    全绿 —— 已实测）。本用例驱动完整 ``execute_bash``：命令串里出现别的
    worktree 落点 ⇒ 必须被拒、``blocked=True``、且**不得**真的跑出去。

    回滚探针：删掉 execute_bash 入口的 ``precheck`` 调用即转红。"""
    from hiveweave.tools import bash as bash_mod

    monkeypatch.setattr(bash_mod, "_pwsh_is_effective_shell", lambda: False)
    ws = tmp_path / ".hiveweave" / "worktrees" / "A044"
    ws.mkdir(parents=True)
    out = await bash_mod.execute_bash(
        command="cat .hiveweave/worktrees/A045/.hiveweave/shared/x.md",
        workdir=str(ws),
        workspace_path=str(ws),
    )
    assert out["success"] is False
    assert out["blocked"] is True
    assert out["fact"] == "runner_failed"
    assert "授权树根" in (out["error"] or "")
    assert "A045" in (out["error"] or "")  # 处方里点名越出的是哪棵树


@pytest.mark.asyncio
async def test_execute_run_command_shares_the_same_precheck(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``run_command`` 是 bash 的**逃生口** ⇒ 必须与 ``execute_bash`` 同链。

    守卫只在 file 侧不算 enforcement。回滚探针：删掉 run_command 里的
    ``precheck`` 调用即转红。"""
    from hiveweave.tools import bash as bash_mod

    monkeypatch.setattr(bash_mod, "_pwsh_is_effective_shell", lambda: False)
    ws = tmp_path / ".hiveweave" / "worktrees" / "A044"
    ws.mkdir(parents=True)
    out = await bash_mod.execute_run_command(
        command="cat .hiveweave/worktrees/A045/.hiveweave/shared/x.md",
        cwd=str(ws),
        timeout_ms=30_000,
        workspace_path=str(ws),
    )
    assert out["success"] is False
    assert out["blocked"] is True
    assert "授权树根" in (out["error"] or "")


# ── ④ 失败出口统一 cwd_display 头（多树归因必要条件）──────────────


@pytest.mark.asyncio
async def test_cwd_display_is_actually_wired_onto_spawn_entries() -> None:
    """**接线守卫**：``cwd_display`` 必须真的装饰在 spawn 入口上。

    只测装饰器函数本身不够 —— 定义了却**没人用**是实测踩过的坑
    （装饰器存在但两个入口都裸着，`[worktree …]` 头一个都不出）。
    回滚探针：摘掉任一入口上的 ``@with_cwd_display`` 即转红。"""
    from hiveweave.tools import bash as bash_mod

    assert hasattr(bash_mod.execute_bash, "__wrapped__"), \
        "execute_bash 未被 with_cwd_display 装饰"
    assert hasattr(bash_mod.execute_run_command, "__wrapped__"), \
        "execute_run_command 未被 with_cwd_display 装饰"
    # 端到端：失败出口必须带上树归因头（多树归因必要条件，fixplan §10.5）
    ws = r"D:\proj\.hiveweave\worktrees\A044"
    out = await bash_mod.execute_bash(command="", workdir=ws, workspace_path=ws)
    assert out["success"] is False
    assert "[worktree A044" in (out["error"] or "")


@pytest.mark.asyncio
async def test_cwd_display_decorator_adds_tree_head_on_failure() -> None:
    @with_cwd_display
    async def _fail(**kwargs):
        return {"success": False, "output": "", "error": "Error: boom"}

    out = await _fail(workdir=r"D:\proj\.hiveweave\worktrees\A044")
    assert out["success"] is False
    assert "[worktree A044" in out["error"]
    assert "D:" not in out["error"]      # 绝不 dump 绝对路径


@pytest.mark.asyncio
async def test_cwd_display_decorator_is_idempotent_and_success_passthrough() -> None:
    @with_cwd_display
    async def _mixed(**kwargs):
        return {"success": False, "output": "",
                "error": "Error: boom\n[MAIN project root] — relative paths"}

    out = await _mixed(workdir=r"D:\proj")
    # 已有回执头 → 不重复追加
    assert out["error"].count("[MAIN") == 1

    @with_cwd_display
    async def _ok(**kwargs):
        return {"success": True, "output": "done", "error": None}

    ok = await _ok(workdir=r"D:\proj\.hiveweave\worktrees\A044")
    assert ok["output"] == "done"
    assert ok["error"] is None


# ── P2-4.1 归因必须指向真病根（2026-09-12） ────────────────────────
#
# 病因：`x=$(head -3 f)` 段首是 `x=$(head`，既不像纯 unix-only 命令名，
# 又让 `_segment_head_token` 把首 token 认成 `-3` ⇒ **内层真病根从未被检查**。
# 旧实现命中了「环境变量前缀」正则（`bash.py:1475`），于是给出
# 「改成 $env:X=…」的处方 —— 而 `$env:X=$(head -3 f)` 里 `head` 照样不存在，
# **agent 按提示改完仍然失败**，白烧一轮，正是这条 gate 本来要避免的。
#
# 判据（**不引 DSH 实现**）：DSH 不做方言翻译、连这个 gate 都没有
# （decision-brief §4.1）—— 照它抄 = 取消 gate = 收到 pwsh 原生报错白烧一轮。
# 只借它的同族判据：`packages/AGENTS.md:117`「never silently skip a missing
# referent」⇒ **给出的原因必须是真原因**。


@pytest.mark.parametrize(
    "command",
    [
        "x=$(head -3 f)",
        "x=$(tail -5 f)",
        "x=$(wc -l f)",
        "OUT=`sed -n 1p f`",
        "x=$(head -3 $(cat f))",  # 嵌套
    ],
)
def test_inner_unix_cmd_is_blamed_not_env_prefix(command: str) -> None:
    """命令替换内的 unix 命令 → 归因到 **它**，不得归因成环境变量前缀。"""
    from hiveweave.tools.bash import detect_untranslated_unix

    msg = detect_untranslated_unix(command)
    assert msg is not None, f"整条漏网（内层命令未被检查）：{command!r}"
    assert "environment-prefix" not in msg, (
        "误归因：内层真病根是 unix 命令，却报了环境变量前缀处方 —— "
        f"agent 照改仍会失败。命令={command!r} 文案={msg!r}"
    )
    assert "unix-only command(s)" in msg, f"未落到 unix 命令分支：{msg!r}"
    # 处方必须点名真病根命令
    assert "head" in msg or "tail" in msg or "wc" in msg or "sed" in msg, (
        f"文案未点名内层命令：{msg!r}"
    )


def test_env_prefix_without_substitution_still_rejected() -> None:
    """反向保护：**没有**命令替换的真·环境前缀写法，仍必须报环境前缀。"""
    from hiveweave.tools.bash import detect_untranslated_unix

    for cmd in ("VAR=val cmd", "FOO=bar echo hi"):
        msg = detect_untranslated_unix(cmd)
        assert msg is not None, f"真·环境前缀漏放行：{cmd!r}"
        assert "environment-prefix" in msg, f"归因丢失：{cmd!r} → {msg!r}"


def test_cmd_substitution_bodies_boundaries() -> None:
    """`_cmd_substitution_bodies` 边界：无替换/不平衡括号 → 空，不得抛异常。"""
    from hiveweave.tools.bash import _cmd_substitution_bodies

    assert _cmd_substitution_bodies("head -3 f") == []
    assert _cmd_substitution_bodies("x=$(unclosed") == []  # 不平衡 → 丢弃
    assert _cmd_substitution_bodies("x=$(head -3 f)") == ["head -3 f"]
    assert _cmd_substitution_bodies("a=`wc -l f` b") == ["wc -l f"]
    # 嵌套取外层整体；内层由后续段扫描展开覆盖
    assert _cmd_substitution_bodies("x=$(head -3 $(cat f))") == [
        "head -3 $(cat f)"
    ]
