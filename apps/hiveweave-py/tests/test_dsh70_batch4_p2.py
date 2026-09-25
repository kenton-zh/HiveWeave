"""TEST_DSH_70 批4「其余 P2」修复钉子（2026-09-25 定案，§2 + §6）。

覆盖五项，每项至少一正一反：
- P2-2  pwsh 方言处方：$LASTEXITCODE 提示词 + 自测失败不计方言口径；
        detect_untranslated_unix **不许被触碰**（§5 禁止施工 #5）。
- P2-4  worktree husk 具名恢复码 + 持锁者探针 + 迁移回写。
- P2-5  org_paradigm 死字段：后端兼容别名（orgParadigm → orgPattern）。
- P2-9③ 目录 miss 枚举同级实际目录 + 相似名猜测，不再讲无关跨树故事。
- P2-1  R7 签名广播：无解法不广播空态 + verified 回填（正文权威）+
        显式幂等键（签名 id，非正文哈希）+ 同签名节流。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hiveweave.services import failure_signature as fs_mod

# ── P2-2：pwsh 方言处方（提示词层）───────────────────────────────────


def test_p2_2_positive_dialect_section_carries_lastexitcode_prescription():
    """正：方言段固定「判成败看 $LASTEXITCODE」+ if(<cmd>) 等价处方。"""
    from hiveweave.prompts.executor import _SHELL_DIALECT_SECTION

    assert "$LASTEXITCODE" in _SHELL_DIALECT_SECTION
    assert "if (git cat-file -e" in _SHELL_DIALECT_SECTION, (
        "必须点名本次实锤的 `if (git cat-file -e …)` 陷阱形态"
    )
    # 归因口径：command_failed（自测失败）不计方言问题
    assert "command_failed" in _SHELL_DIALECT_SECTION
    assert "不是方言问题" in _SHELL_DIALECT_SECTION


def test_p2_2_positive_section_injected_into_both_shell_roles():
    """正：处方随方言段注入 test_engineer 与 generic executor 两个角色。"""
    from hiveweave.prompts.executor import (
        _SHELL_DIALECT_SECTION,
        _generic_executor_script,
        _test_engineer_script,
    )

    assert _SHELL_DIALECT_SECTION in _test_engineer_script("测试工程师")
    assert _SHELL_DIALECT_SECTION in _generic_executor_script("dev", "A001")


def test_p2_2_negative_detect_untranslated_unix_untouched():
    """反（禁止施工 #5）：检测代码路径不被本批触碰。

    判据：`detect_untranslated_unix` 只存在于 tools/bash.py 的检测侧，
    方言段文本不得引用/改写它（修法明确只动提示词文本）。
    """
    from hiveweave.prompts.executor import _SHELL_DIALECT_SECTION
    from hiveweave.tools import bash as bash_mod

    assert hasattr(bash_mod, "detect_untranslated_unix"), (
        "检测函数必须原样存在"
    )
    assert "detect_untranslated_unix" not in _SHELL_DIALECT_SECTION
    src = Path(bash_mod.__file__).read_text(encoding="utf-8")
    assert "$LASTEXITCODE" not in src, (
        "检测代码里不得混入提示词处方（提示词层修复，不动检测）"
    )


# ── P2-4：worktree husk 具名恢复码 + 持锁者探针 ──────────────────────


def test_p2_4_positive_named_recovery_codes_exist():
    """正：具名恢复码是包级常量（可 grep、可断言），不是散落文案。"""
    from hiveweave.services.git_worktree import constants as wt_const

    for code in (
        "WT_HUSK_REPAIR_REFUSED",
        "WT_HUSK_REPAIR_REGAINED",
        "WT_HUSK_REPAIR_RM_LOCKED",
        "WT_HUSK_REPAIR_ADD_FAILED",
        "WT_STALE_PATH_RELOCATED",
    ):
        assert getattr(wt_const, code) == code


@pytest.mark.asyncio
async def test_p2_4_positive_rm_locked_returns_named_code_and_holders(
    tmp_path, monkeypatch
):
    """正：husk 删除失败（目录仍在）⇒ WT_HUSK_REPAIR_RM_LOCKED + 持锁者探针。

    判据是**目录仍在**（状态），不是 rmtree 的 rc —— 猿猴补丁把 rmtree 变
    no-op 正是 Windows Device busy 的同构现场。
    """
    import shutil as _shutil

    from hiveweave.services.git_worktree.paths import (
        _worktree_binding_under_project,
    )
    from hiveweave.services.git_worktree.service_merge import MergeMixin

    root = tmp_path / "proj"
    ws = root / ".hiveweave" / "worktrees" / "A469"
    ws.mkdir(parents=True)
    assert _worktree_binding_under_project(str(ws), str(root))

    holders = [{"pid": 4321, "port": 5199, "cwd": str(ws), "command": "vite"}]

    def _fake_rmtree(path, *a, **kw):  # Device busy：目录删不掉
        return None

    async def _noop_async(*a, **kw):
        return None

    monkeypatch.setattr(_shutil, "rmtree", _fake_rmtree)
    monkeypatch.setattr(
        "hiveweave.services.process_registry.probe_processes_for_worktree",
        lambda p: holders,
        raising=False,
    )
    monkeypatch.setattr(
        "hiveweave.services.process_registry.stop_processes_for_worktree",
        lambda p: {"stopped": [], "failed": []},
        raising=False,
    )
    monkeypatch.setattr(
        "hiveweave.services.acl_sandbox.service.unlock_git_lockdown",
        lambda root: 0,
        raising=False,
    )
    monkeypatch.setattr(
        "hiveweave.services.git_worktree.service_merge._git", _noop_async
    )

    svc = MergeMixin()
    err, diag = await svc._auto_repair_husk(
        str(root), "A469", str(ws), "hw/A469/work"
    )
    assert err is not None
    assert "WT_HUSK_REPAIR_RM_LOCKED" in err, "失败必须带具名恢复码"
    assert "did NOT succeed" in err, "「修不了就别宣称在修」：不得假宣称在修"
    assert diag["code"] == "WT_HUSK_REPAIR_RM_LOCKED"
    assert diag["holders"] == holders, "必须随附持锁者探针结果"
    assert "pid=4321" in err, "持锁者必须出现在可操作文案里"


@pytest.mark.asyncio
async def test_p2_4_negative_binding_refused_never_deletes(tmp_path):
    """反：目标不是本项目 worktrees 下的绑定目录 ⇒ REFUSED，且不删任何东西。"""
    from hiveweave.services.git_worktree.service_merge import MergeMixin

    root = tmp_path / "proj"
    root.mkdir(parents=True)
    outside = tmp_path / "outside" / "A469"
    outside.mkdir(parents=True)

    svc = MergeMixin()
    err, diag = await svc._auto_repair_husk(
        str(root), "A469", str(outside), "hw/A469/work"
    )
    assert err is not None
    assert diag["code"] == "WT_HUSK_REPAIR_REFUSED"
    assert outside.exists(), "拒绝路径不得有删除动作"
    assert "holders" in diag


def test_p2_4_negative_unlock_wired_before_destructive_path():
    """反（AGENTS.md 锁死契约）：husk 修复/husk 清理路径必须先解锁再删。

    AST 判据（忽略 docstring 提及）：`_auto_repair_husk` 与
    reconcile._rmtree_husk 函数体内 unlock 调用的行号必须先于
    shutil.rmtree 调用 —— 封条后平台无 DELETE/DC，不解锁就 PermissionError
    或 rc=0 假成功。
    """
    import ast

    merge_src = Path(
        __import__("hiveweave.services.git_worktree.service_merge", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    rec_src = Path(
        __import__("hiveweave.services.git_worktree.reconcile", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")

    def _unlock_before_rmtree(src: str, fn_name: str) -> bool:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == fn_name
            ):
                unlock_lines, rmtree_lines = [], []
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        callee = sub.func
                        name = (
                            callee.attr
                            if isinstance(callee, ast.Attribute)
                            else getattr(callee, "id", "")
                        )
                        if name == "unlock_git_lockdown":
                            unlock_lines.append(sub.lineno)
                        if (
                            isinstance(callee, ast.Attribute)
                            and callee.attr == "rmtree"
                            and isinstance(callee.value, ast.Name)
                            and callee.value.id == "shutil"
                        ):
                            rmtree_lines.append(sub.lineno)
                return bool(unlock_lines and rmtree_lines
                            and min(unlock_lines) < min(rmtree_lines))
        return False

    assert _unlock_before_rmtree(merge_src, "_auto_repair_husk")
    assert _unlock_before_rmtree(rec_src, "_rmtree_husk")


def test_p2_4_positive_relocation_writeback_deprecates_original():
    """正：迁移回写 —— rebuild 事件带 original_deprecated 事实位。"""
    import inspect

    from hiveweave.services.git_worktree import reconcile

    sig = inspect.signature(reconcile._log_worktree_rebuild_event)
    assert "original_deprecated" in sig.parameters, (
        "rebuild 事件必须能落「原树已废弃」事实位"
    )
    # service_create 的两个迁移点都传 original_deprecated=True
    create_src = Path(
        __import__("hiveweave.services.git_worktree.service_create", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    assert create_src.count("original_deprecated=True") == 2


# ── P2-5：org_paradigm 死字段 —— 后端兼容别名 ────────────────────────


def test_p2_5_positive_orgparadigm_alias_lands_in_charter():
    """正：前端键名 orgParadigm 被接受并映射到 charter.orgPattern。"""
    from hiveweave.api.projects import ProjectCreate, _build_charter_dict

    body = ProjectCreate(
        name="p", workspacePath="C:/tmp/ws", orgParadigm="hierarchy"
    )
    charter = _build_charter_dict(body)
    assert charter["orgPattern"] == "hierarchy", (
        "前端写不进去（pydantic 静默丢弃）= P2-5 病根，必须能落"
    )


def test_p2_5_negative_explicit_orgpattern_wins_and_default_solo():
    """反：显式 orgPattern 优先于别名；两者都缺仍落 'solo'。"""
    from hiveweave.api.projects import ProjectCreate, _build_charter_dict

    body = ProjectCreate(
        name="p", workspacePath="C:/tmp/ws",
        orgPattern="flat", orgParadigm="hierarchy",
    )
    assert _build_charter_dict(body)["orgPattern"] == "flat"

    body2 = ProjectCreate(name="p", workspacePath="C:/tmp/ws")
    assert _build_charter_dict(body2)["orgPattern"] == "solo"


# ── P2-9③：目录 miss 枚举同级实际目录 ───────────────────────────────


def _make_tree(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    for d in ("src", "docs", "tests-e2e", "scripts"):
        (root / d).mkdir(parents=True)
    (root / "src" / "app.py").write_text("x", encoding="utf-8")
    return root


@pytest.mark.asyncio
async def test_p2_9_positive_list_files_miss_enumerates_siblings(tmp_path):
    """正：目录 miss 的 hint 枚举同级实际存在的目录 + 相似名猜测。"""
    from hiveweave.tools.file import list_files

    root = _make_tree(tmp_path)
    out = await list_files("tests", str(root), project_root=str(root))
    assert out["success"] is False
    err = out["error"] or ""
    assert "Existing directories" in err
    for name in ("docs", "scripts", "src", "tests-e2e"):
        assert name in err, f"同级目录 {name} 必须被枚举"
    assert "tests-e2e" in err and "Possible name match" in err, (
        "存在相似名（tests → tests-e2e）⇒ 猜测提示"
    )
    assert "does not exist in this tree" in err


@pytest.mark.asyncio
async def test_p2_9_positive_missing_parent_chain_still_enumerates(tmp_path):
    """正：父目录整段不存在时，向上找到最近存在祖先再枚举。"""
    from hiveweave.tools.file import list_files

    root = _make_tree(tmp_path)
    out = await list_files("tests/unit/x", str(root), project_root=str(root))
    assert out["success"] is False
    err = out["error"] or ""
    assert "Existing directories" in err, "最近存在祖先（项目根）必须被枚举"
    assert "src" in err


@pytest.mark.asyncio
async def test_p2_9_negative_generic_miss_drops_cross_tree_story(tmp_path):
    """反：普通目录 miss 不再贴 .hiveweave/shared 跨树话术（场景无关）。"""
    from hiveweave.tools.file import list_files

    root = _make_tree(tmp_path)
    out = await list_files("tests", str(root), project_root=str(root))
    err = out["error"] or ""
    assert ".hiveweave/shared/" not in err, (
        "READ_MISS_HINT 的跨树故事与 tests/src 场景无关，不得再贴"
    )
    assert "cross-tree" not in err


@pytest.mark.asyncio
async def test_p2_9_negative_shared_subdir_keeps_its_own_hint(tmp_path):
    """反：.hiveweave/shared 子目录 miss 仍走自己的策略话术（那里的跨树
    故事是场景对的），不被枚举式 hint 覆盖。"""
    from hiveweave.tools.file import list_files

    root = _make_tree(tmp_path)
    (root / ".hiveweave" / "shared").mkdir(parents=True)
    out = await list_files(
        ".hiveweave/shared/missing-dir", str(root), project_root=str(root)
    )
    assert out["success"] is False
    err = out["error"] or ""
    assert "merge=binary" in err, "shared 的 miss 话术必须保留"


# ── P2-1：R7 签名广播（verified 回填 / 显式幂等键 / 节流）────────────


class _FakeSharedSpace:
    """内存版项目共享空间（与 test_failure_signature_* 同款假体）。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def get_project_memories(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self.rows]

    async def save_memory(
        self,
        *,
        agent_id: str,
        project_id: str,
        scope: str,
        content: str,
        type: str = "fact",
        module_id: str | None = None,
        source_agent_id: str | None = None,
        metadata: dict | None = None,
        **_: object,
    ) -> str:
        for r in self.rows:
            if (
                r.get("agent_id") == agent_id
                and r.get("scope") == scope
                and r.get("module_id") == module_id
            ):
                r.update(
                    content=content,
                    type=type,
                    source_agent_id=source_agent_id,
                    metadata=metadata,
                )
                return r["id"]
        mid = f"mem-{len(self.rows) + 1}"
        self.rows.append(
            {
                "id": mid,
                "agent_id": agent_id,
                "project_id": project_id,
                "scope": scope,
                "module_id": module_id,
                "type": type,
                "content": content,
                "source_agent_id": source_agent_id,
                "metadata": metadata or {},
            }
        )
        return mid


@pytest.fixture
def sig_space():
    fake = _FakeSharedSpace()
    with patch("hiveweave.services.memory.MemoryService", return_value=fake):
        yield fake


_ERROR = (
    "Error: deployment failed at stage assemble with target directory "
    "build-out for module alpha-worker"
)


def _seed_fork_entry(space: _FakeSharedSpace, sig: str) -> None:
    """种一条**历史分叉**条目：正文带「已验证解法:」行、状态位停在 none。"""
    space.rows.append(
        {
            "id": "mem-1",
            "agent_id": fs_mod._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs_mod.make_module_id("proj", sig, "bash"),
            "type": "failure_signature",
            "content": (
                f"[失败签名] tool=bash | {sig}\n"
                "根因提示: 见错误原文\n"
                "已验证解法: 改用 pwsh $LASTEXITCODE 判成败\n"
                f"原文尾: {sig[-30:]}\n"
                "首个撞到的 Agent: agent-A"
            ),
            "source_agent_id": "agent-A",
            "metadata": {
                "signature": sig,
                "tool_name": "bash",
                "solution_status": fs_mod.SOLUTION_STATUS_NONE,
            },
        }
    )


@pytest.mark.asyncio
async def test_p2_1_positive_fork_entry_backfills_verified_and_broadcasts(sig_space):
    """正：有「已验证解法:」行的分叉条目 ⇒ verified 回填 + 解法广播（不再空态）。"""
    sig = fs_mod.signature_of(_ERROR)
    _seed_fork_entry(sig_space, sig)

    notice = await fs_mod.known_signature_notice(
        "proj", _ERROR, agent_id="agent-B", tool_name="bash"
    )
    assert notice is not None
    text, tier = notice
    assert tier == fs_mod.NOTICE_TIER_SOLUTION
    assert "改用 pwsh $LASTEXITCODE 判成败" in text, "解法原文必须广播出去"
    assert "暂无已验证解法" not in text, "有解法不得再广播空态"


@pytest.mark.asyncio
async def test_p2_1_positive_rehit_repairs_fork_status_in_write_path(sig_space):
    """正：写侧 rehit 用正文推导状态**就地修复**分叉（机检不变式收敛）。"""
    sig = fs_mod.signature_of(_ERROR)
    _seed_fork_entry(sig_space, sig)

    rec = await fs_mod.record_failure_signature(
        project_id="proj",
        agent_id="agent-B",
        tool_name="bash",
        error=_ERROR,
        attribution="",
    )
    assert rec["written"] and rec["preexisting"]
    row = sig_space.rows[0]
    assert row["metadata"]["solution_status"] == fs_mod.SOLUTION_STATUS_VERIFIED, (
        "正文带已验证解法行 ⇒ rehit 后状态位必须是 verified"
    )
    # 解法行必须随 rehit 携带（覆写不冲掉回填）
    assert "已验证解法: 改用 pwsh $LASTEXITCODE 判成败" in row["content"]


@pytest.mark.asyncio
async def test_p2_1_positive_retried_ok_entry_broadcasts_neutral_tier(sig_space):
    """正：retried_ok 回声条目 ⇒ 中性提示（不冒充已验证解法）。"""
    sig = fs_mod.signature_of(_ERROR)
    sig_space.rows.append(
        {
            "id": "mem-2",
            "agent_id": fs_mod._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs_mod.make_module_id("proj", sig, "bash"),
            "type": "failure_signature",
            "content": (
                f"[失败签名] tool=bash | {sig}\n"
                "根因提示: 见错误原文\n"
                "同参重试: 此前有 Agent 以完全相同参数重试成功\n"
                f"原文尾: {sig[-30:]}\n"
                "首个撞到的 Agent: agent-A"
            ),
            "source_agent_id": "agent-A",
            "metadata": {
                "signature": sig,
                "tool_name": "bash",
                "solution_status": fs_mod.SOLUTION_STATUS_NONE,
            },
        }
    )
    notice = await fs_mod.known_signature_notice(
        "proj", _ERROR, agent_id="agent-B", tool_name="bash"
    )
    assert notice is not None
    text, tier = notice
    assert tier == fs_mod.NOTICE_TIER_RETRY_ECHO
    assert "同参重试曾成功" in text


@pytest.mark.asyncio
async def test_p2_1_negative_none_entry_never_broadcasts_empty_state(sig_space):
    """反：solution_status=none 且无解法行 ⇒ 不广播（返回 None）。"""
    sig = fs_mod.signature_of(_ERROR)
    sig_space.rows.append(
        {
            "id": "mem-3",
            "agent_id": fs_mod._SIGNATURE_WRITER,
            "scope": "project",
            "module_id": fs_mod.make_module_id("proj", sig, "bash"),
            "type": "failure_signature",
            "content": (
                f"[失败签名] tool=bash | {sig}\n"
                "根因提示: 见错误原文\n"
                f"原文尾: {sig[-30:]}\n"
                "首个撞到的 Agent: agent-A"
            ),
            "source_agent_id": "agent-A",
            "metadata": {
                "signature": sig,
                "tool_name": "bash",
                "solution_status": fs_mod.SOLUTION_STATUS_NONE,
            },
        }
    )
    assert await fs_mod.known_signature_notice(
        "proj", _ERROR, agent_id="agent-B", tool_name="bash"
    ) is None
    assert await fs_mod.known_signature_hint(
        "proj", _ERROR, agent_id="agent-B", tool_name="bash"
    ) is None


def test_p2_1_negative_content_derived_status_rejects_empty_solution_line():
    """反：空的「已验证解法:」行不算 verified（防占位行抬档位）。"""
    assert fs_mod._content_derived_status("已验证解法:") is None
    assert (
        fs_mod._content_derived_status("已验证解法: \n同参重试: x")
        == fs_mod.SOLUTION_STATUS_RETRIED_OK
    )
    assert (
        fs_mod._content_derived_status("已验证解法: 真解法")
        == fs_mod.SOLUTION_STATUS_VERIFIED
    )
    assert fs_mod._content_derived_status("根因提示: 见错误原文") is None


@pytest.mark.asyncio
async def test_p2_1_positive_deliver_notice_passes_explicit_idempotency_key():
    """正：deliver_notice 显式幂等键透传 inbox（签名 id，非正文哈希）。"""
    from hiveweave.services import health_notice as hn
    from unittest.mock import MagicMock

    svc = MagicMock()
    svc.send_message = AsyncMock(return_value={"id": "m1"})
    with patch("hiveweave.services.inbox.InboxService", return_value=svc):
        ok = await hn.deliver_notice(
            "agent-A", "text", kind=hn.KIND_SHARED_FIX,
            idempotency_key="sigfix|agent-A|bash|abc|solution",
        )
    assert ok is True
    kw = svc.send_message.await_args.kwargs
    assert kw["idempotency_key"] == "sigfix|agent-A|bash|abc|solution"


@pytest.mark.asyncio
async def test_p2_1_negative_deliver_without_key_leaves_content_hash_path():
    """反：不传键 ⇒ idempotency_key=None（inbox 落正文哈希，旧行为不回归）。"""
    from hiveweave.services import health_notice as hn
    from unittest.mock import MagicMock

    svc = MagicMock()
    svc.send_message = AsyncMock(return_value={"id": "m1"})
    with patch("hiveweave.services.inbox.InboxService", return_value=svc):
        await hn.deliver_notice("agent-A", "text", kind=hn.KIND_SELF_REPEAT)
    kw = svc.send_message.await_args.kwargs
    assert kw["idempotency_key"] is None


@pytest.mark.asyncio
async def test_p2_1_positive_f10_hooks_keys_stable_while_body_varies():
    """正：正文时变（#N / X 秒前）但幂等键**逐字节稳定** —— 节流生效。

    病根重放：同 agent 短窗内复撞，SELF REPEAT 正文嵌 "#2 / 0 秒前" →
    "#3 / 0 秒前"，正文哈希键逐条不同 ⇒ 47 条通知去重全失效。修复后键 =
    (接收人, 工具, 签名 id, 档位/时间桶)，正文变了键不变。
    （首撞按设计只登记不发 SELF REPEAT（TEST_DSH_47 #6 语义），故首两撞
    建立状态，第三撞起比较。）
    """
    from hiveweave.services import failure_signature as fsig
    from hiveweave.tools import executor as exec_mod

    delivered: list[dict] = []

    async def _fake_deliver(agent_id, text, *, kind, **kw):
        delivered.append({"agent": agent_id, "text": text, "kind": kind, **kw})
        return True

    async def _fake_record(**kw):
        return {
            "written": True,
            "preexisting": True,
            "preexisting_source": "agent-A",
            "sig": "SIG|STABLE|SIGNATURE|TEXT|THAT|IS|LONG|ENOUGH|XX",
            "module_id": "mid-1",
        }

    async def _fake_notice(*a, **kw):
        return ("[shared fix] sol text", fsig.NOTICE_TIER_SOLUTION)

    async def _fake_org(**kw):
        return ""

    err = "Error: Command blocked: [unattended mode] something long enough"
    with (
        patch("hiveweave.tools.executor.deliver_notice", _fake_deliver),
        patch("hiveweave.db.meta.get_agent_project_id", AsyncMock(return_value="proj-1")),
        patch.object(fsig, "record_failure_signature", _fake_record),
        patch.object(fsig, "known_signature_notice", _fake_notice),
        patch.object(fsig, "note_distinct_hitter", _fake_org),
        # 时间桶确定性：窗口放到天文数字 ⇒ 桶号恒 0，测试不跨桶
        patch.object(exec_mod, "_LAST_SEEN_SIG_WINDOW_S", 10 ** 9),
    ):
        exec_mod.reset_self_repeat_hits_for_tests()
        result = {"success": False, "output": "", "error": err}
        for _ in range(3):
            await exec_mod._f10_result_hooks(result, "bash", {"a": 1}, "agent-A")

    fixes = [d for d in delivered if d["kind"] == "SHARED_FIX"]
    selves = [d for d in delivered if d["kind"] == "SELF_REPEAT"]
    assert len(fixes) == 3, "每次复撞都尝试广播（inbox 层再按键去重）"
    assert {f["idempotency_key"] for f in fixes} == {fixes[0]["idempotency_key"]}, (
        "同签名同档位 ⇒ 幂等键逐字节相同（节流）"
    )
    assert "|solution" in fixes[0]["idempotency_key"]
    assert len(selves) == 2, "首撞登记不发，复撞两轮各发一条"
    assert selves[0]["text"] != selves[1]["text"], (
        "前置：SELF REPEAT 正文必须随复撞次数变化（#N）"
    )
    assert selves[0]["idempotency_key"] == selves[1]["idempotency_key"], (
        "正文时变不得击败幂等键（同时间桶内键稳定）"
    )
