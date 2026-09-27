"""批 3（I2）：封条读回收窄（自愈对称 + 爆炸半径裁决）的守卫测试。

对应修复：`services/acl_sandbox/service.py::_seal_git_bootstrap_files`
（`seal_file` 摘除集并上 `git_main_sid` + `_handle_readback_failure` 闭包、
`SealReadbackError`、env `HIVEWEAVE_SEAL_READBACK_DEGRADE`）与
`services/acl_sandbox/telemetry.py`（seal_degraded / seal_degrade_shadow）。

事故背景（09-27 s3-clone_13，PLATFORM-ISSUES I2）：`.git/config` 被 git
lock+rename 换新后**继承回 git_main_sid 写位**，而 seal 的摘除集不含
git_main_sid ⇒ 摘不掉 ⇒ 读回永远失败 ⇒ 整项目 84% 的 shell 永久 fail-closed
（状态在磁盘 ACL，换后端进程无效）。

验收映射（fixplan 批 3 / I2）：
① 自愈对称（修 A，repair 非放宽）：事故态（config 携带 MAIN 写位）经一轮
  `seal_file` 必须摘干净（真 DACL 读回为空）；同时 `.git` 根上批 A 第 0 步
  **故意授的**两条 MAIN ACE 原样存活 —— 根的 seal 刻意不并 git_main_sid，
  不许每轮摘→re-grant 重灌。
② 爆炸半径裁决（修 B，放宽类带 shadow）：读回失败按**状态判据**裁决 ——
  泄漏 SID 与本命令受限令牌（`policy.write_sids`）**有交集 ⇒ 任何档位
  fail-closed**（MAIN 令牌携带 git_main_sid）；无交集（叶子令牌）：
  `enforce` 降级放行（telemetry 计数 + warning 行；不进 changed —— 审计 P2-1）+
  `telemetry.seal_degraded_count`）、`shadow` 只观测仍 fail-closed
  （`seal_degrade_shadow_count`）、`off` 恒 fail-closed、**缺省（env 未设）
  等价 shadow**。
③ 类型契约：`SealReadbackError` 是 `SandboxUnavailableError` 子类、
  `platform_side=True`（经 `is_platform_side` 判据为 True ⇒ 下游统一盖
  runner_failed 戳的通道不受影响）、`leaking_sids` 有序、`seal_path` 保真。

上游对照：
- DSH ``docs/testing.md:40`` —— guard 只有能被 regression 打红才算守卫。
- 阳性对照（施工轮已执行，见各组用例 docstring）：把 `seal_file` 的摘除集
  并集改回 `sids` ⇒ 组 1「读回干净」断言转红（config 上 MAIN 写位残留 ⇒
  缺省 shadow 档 raise）；把 `_handle_readback_failure` 的交集短路
  （`_seal_leak_intersects_token → raise`）删掉 ⇒ 组 2 的 MAIN 用例转红。
- 风险序（fixplan §一）：先保护（交集恒 fail-closed）、再观测（shadow）、
  才放宽（enforce）；`off` 是应急回滚闸，三档并存由本文件钉死。

仅 Windows 运行：组 1 走**真 ACL**（`WriteGrant` 落在 tmp 文件上），组 2 的
`_grant_if_missing` / `deny_dc` 亦走真 ACL（`@pytest.mark.win32`）。
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hiveweave.services.acl_sandbox import service as svc
from hiveweave.services.acl_sandbox import telemetry
from hiveweave.services.acl_sandbox.errors import (
    SandboxUnavailableError,
    is_platform_side,
)
from hiveweave.services.acl_sandbox.grant import (
    ACE_ALLOWED,
    GIT_MAIN_CREATE_MASK,
    GRANT_MASK,
    _INHERIT_ONLY_ACE,
    WriteGrant,
)
from hiveweave.services.acl_sandbox.policy import resolve_policy
from hiveweave.services.acl_sandbox.sid import git_main_sid, git_sid

pytestmark = [pytest.mark.win32]

if not sys.platform.startswith("win"):
    pytest.skip("I2 封条读回收窄守卫需要真 ACL（Windows），非 Windows 跳过",
                allow_module_level=True)

pytest.importorskip("win32api")


# ══════════════════════════════════════════════════════════════════
# 基座：假 git 布局 + 全局态复位
# ══════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _seal_globals_reset(monkeypatch):
    """模块级全局态复位：`_WORKTREE_CONFIG_RETIRED` 是进程级 set（测试间必须
    reset）；档位 env 先剥到未设（缺省 = shadow），各用例再自行 setenv。"""
    monkeypatch.delenv(svc.SEAL_READBACK_DEGRADE_ENV, raising=False)
    svc._WORKTREE_CONFIG_RETIRED.clear()
    yield
    svc._WORKTREE_CONFIG_RETIRED.clear()


@pytest.fixture(autouse=True)
def _telemetry_reset():
    """遥测计数器是进程级单例 —— 每用例前后归零，断言才可信。"""
    telemetry.reset_for_tests()
    yield
    telemetry.reset_for_tests()


@pytest.fixture(autouse=True)
def _retire_worktree_config_mocked(monkeypatch):
    """① 步退休扩展会跑真 `git config` —— 隔离成 AsyncMock（返回成功）。

    service 在函数体内延迟 import 该符号 ⇒ patch 源模块属性即可生效。
    """
    monkeypatch.setattr(
        "hiveweave.services.git_worktree.git_identity.retire_worktree_config",
        AsyncMock(return_value=True),
    )


@pytest.fixture
def layout(tmp_path):
    """手工假 git 布局（不起真 git）：

    <proj>/.git/{config,hooks/,objects/,refs/,logs/} + 叶子 <ws>（含 gitdir
    指针文件，指向 <proj>/.git/worktrees/<ws-name> —— `_seal_git_bootstrap_files`
    对叶子边界的指针身份判据要求）。
    """
    proj = tmp_path / "proj"
    ws = tmp_path / "ws"
    git_dir = proj / ".git"
    for d in (proj, ws, git_dir, git_dir / "hooks",
              git_dir / "objects", git_dir / "refs", git_dir / "logs"):
        d.mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text("", encoding="utf-8")
    expected_gitdir = git_dir / "worktrees" / ws.name
    (ws / ".git").write_text(f"gitdir: {expected_gitdir}\n", encoding="utf-8")

    proj_real = os.path.realpath(str(proj))
    ws_real = os.path.realpath(str(ws))
    box = SimpleNamespace(
        proj=proj, ws=ws, git_dir=git_dir, config=git_dir / "config",
        proj_real=proj_real, ws_real=ws_real,
        main_sid=git_main_sid(proj_real),
    )
    yield box
    # 清理侧：组 1 的锁死档会让 config 删不掉（own DELETE 被摘 + 父目录 FC
    # deny）—— 走生产清理路径解锁，别给 pytest tmp 回收留刺（best-effort）。
    for root in (proj_real, ws_real):
        try:
            svc.unlock_git_lockdown(root)
        except Exception:
            pass


def _leaf_policy(layout):
    """叶子形态（ws != proj）：令牌**不携带** git_main_sid（build_write_sids
    仅 MAIN 边界（boundary == project）并入 —— policy.py:288-332）。"""
    return resolve_policy(workspace_path=str(layout.ws), agent_id="a1",
                          entry="bash", project_workspace_path=str(layout.proj))


def _main_policy(layout):
    """MAIN 形态（ws == proj）：令牌**携带** git_main_sid（CEO/HR/bash_main）。"""
    return resolve_policy(workspace_path=str(layout.proj), agent_id="CEO",
                          entry="bash", project_workspace_path=str(layout.proj))


def _normcased_prefix_entries(changed: list[str], prefix: str) -> list[str]:
    return [os.path.normcase(c[len(prefix):]) for c in changed
            if c.startswith(prefix)]


# ══════════════════════════════════════════════════════════════════
# 组 1 —— 自愈对称（修 A）：seal 摘除集并上 git_main_sid
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["leaf", "main"])
async def test_seal_file_strips_git_main_sid(layout, monkeypatch, shape):
    """事故态一轮自愈：config 上继承回的 MAIN 写位被 seal 摘掉、读回干净。

    阳性对照（施工轮执行转红）：把 `seal_file` 的摘除集并集改回 `sids`
    （去掉 `| {git_main_sid(project)}`）⇒ 预置的 MAIN 写位摘不掉 ⇒
    `_agent_aces_leaking(config)` 非空 ⇒ 缺省（shadow）档 raise ⇒
    本用例的「不抛 + 读回干净」断言同步转红。

    全程真 ACL（`_grant_aces` 不 mock）：seal 前真授事故 ACE、seal 后真读回
    —— 「摘了个寂寞」与「已封」在真实 DACL 上必须长得不一样。
    """
    git_dir = str(layout.git_dir)
    config_path = str(layout.config)
    # 事故态（真实 DACL）：git lock+rename 换新 config 后继承回 MAIN 写位
    assert WriteGrant.grant_standing(config_path, layout.main_sid, GRANT_MASK)
    # `.git` 根两条 MAIN ACE（批 A 第 0 步 `grant_git_main_dir_aces` 的形态）
    assert WriteGrant.grant_git_main_dir_aces(git_dir, layout.main_sid)

    calls: list[tuple[str, set[str]]] = []
    orig = svc._AsyncGrant.seal_agent_aces_async

    async def _spy(self, path, sids, *, lock_against_delete: bool = False):
        calls.append((os.path.normcase(path), set(sids)))
        return await orig(self, path, sids,
                          lock_against_delete=lock_against_delete)

    monkeypatch.setattr(svc._AsyncGrant, "seal_agent_aces_async", _spy)

    policy = _leaf_policy(layout) if shape == "leaf" else _main_policy(layout)
    changed = await svc._seal_git_bootstrap_files(
        policy, svc._AsyncGrant(WriteGrant()))

    # config 本轮真的封过（写盘发生过，不是被静默跳过）
    assert os.path.normcase(config_path) in _normcased_prefix_entries(
        changed, "seal:")

    def _recorded_sids(path: str) -> set[str]:
        key = os.path.normcase(path)
        got = [s for p, s in calls if p == key]
        assert got, f"本轮没有任何针对 {path} 的 seal 调用：{[p for p, _ in calls]}"
        return got[0]

    # 修 A 核心：config 的摘除集**包含** git_main_sid（事故 ACE 才摘得掉）
    assert layout.main_sid in _recorded_sids(config_path)
    # 根不并：`.git` 根的 seal 调用 sids **不含** git_main_sid —— 根上两条
    # MAIN ACE 是批 A 故意授的，并进去会每轮摘→re-grant 重灌（急切传播很贵）
    assert layout.main_sid not in _recorded_sids(git_dir)

    # 状态判据（真 DACL）：config 读回干净 —— MAIN 写位真的被摘掉了
    assert svc._agent_aces_leaking(config_path) == []
    assert not any(s == layout.main_sid
                   for _t, _f, _m, s in WriteGrant.list_aces(config_path))
    # 根上两条 MAIN ACE 原样存活（非继承创建位 + OI|IO 继承数据位）
    root_aces = {(m, f) for _t, f, m, s in WriteGrant.list_aces(git_dir)
                 if s == layout.main_sid}
    assert (GIT_MAIN_CREATE_MASK, 0) in root_aces, (
        f"`.git` 根的非继承创建位被 seal 摘掉（根不许并 git_main_sid）："
        f"{root_aces}")
    assert any(f & _INHERIT_ONLY_ACE and m == GRANT_MASK
               for m, f in root_aces), (
        f"`.git` 根的 OI|IO 数据位被 seal 摘掉（根不许并 git_main_sid）："
        f"{root_aces}")


# ══════════════════════════════════════════════════════════════════
# 组 2 —— 爆炸半径裁决（修 B）：读回失败按状态判据分档
# ══════════════════════════════════════════════════════════════════


def _stuck_state(monkeypatch, layout) -> None:
    """构造「seal 摘不掉」的卡死态（事故形态）：

    - `seal_agent_aces_async` no-op（返回 False，什么都不摘 —— 摘除集缺
      MAIN ⇒ 摘了个寂寞）；
    - `_grant_aces` 让 config 恒返回 MAIN 写位 ACE（读回永远失败），其余
      路径（`.git` 根 / hooks / 占位载体 / 指针）干净 —— 避免无关失败。
    """
    config_path = os.path.normcase(str(layout.config))

    async def _cannot_seal(self, path, sids, *, lock_against_delete: bool = False):
        return False

    monkeypatch.setattr(svc._AsyncGrant, "seal_agent_aces_async", _cannot_seal)

    def _fake_aces(path: str) -> list[tuple[int, int, int, str]]:
        if os.path.normcase(path) == config_path:
            return [(ACE_ALLOWED, 0, GRANT_MASK, layout.main_sid)]
        return []

    monkeypatch.setattr(svc, "_grant_aces", _fake_aces)


@pytest.mark.asyncio
async def test_enforce_no_intersection_degrades_and_passes(layout, monkeypatch):
    """enforce + 叶子令牌（与泄漏 SID 无交集）⇒ 降级放行，不抛。

    批 3 审计 P2-1：降级放行**不进** `changed`（那是「本次实际改动」的语义，
    混入失败记录会污染 `_remember_sealed`/`git_bootstrap_sealed` 日志）——
    放行证据 = telemetry 计数 + `seal_degraded_pass` warning 行。

    阳性对照：删掉 `_handle_readback_failure`（恢复成无条件 raise）⇒
    本用例转红；把状态判据换成恒 True（有交集）⇒ 同样转红。
    """
    _stuck_state(monkeypatch, layout)
    monkeypatch.setenv(svc.SEAL_READBACK_DEGRADE_ENV, "enforce")
    policy = _leaf_policy(layout)

    changed = await svc._seal_git_bootstrap_files(
        policy, svc._AsyncGrant(WriteGrant()))

    assert not _normcased_prefix_entries(changed, "degraded:"), changed
    snap = telemetry.snapshot()
    assert snap["seal_degraded_count"] == 1
    assert snap["seal_degrade_shadow_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["enforce", "shadow", "off", "default"])
async def test_main_intersection_always_fail_closed(layout, monkeypatch, mode):
    """MAIN 令牌**携带** git_main_sid（与泄漏 SID 有交集）⇒ 任何档位都 fail-closed。

    交集短路排在档位分支之前 ⇒ 连 shadow 遥测都不记（这不是"本可降级"的
    形态 —— 该令牌真能写封条面）。缺省档（env 未设）同样 fail-closed。

    阳性对照：把 `_handle_readback_failure` 的交集短路删掉 ⇒ enforce 档会把
    MAIN 令牌降级放行（放大风险）⇒ 本用例转红。
    """
    _stuck_state(monkeypatch, layout)
    if mode != "default":
        monkeypatch.setenv(svc.SEAL_READBACK_DEGRADE_ENV, mode)
    policy = _main_policy(layout)

    with pytest.raises(svc.SealReadbackError) as ei:
        await svc._seal_git_bootstrap_files(policy, svc._AsyncGrant(WriteGrant()))

    assert ei.value.leaking_sids == [layout.main_sid]
    snap = telemetry.snapshot()
    assert snap["seal_degraded_count"] == 0
    assert snap["seal_degrade_shadow_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "off", "default"])
async def test_leaf_no_intersection_mode_matrix(layout, monkeypatch, mode):
    """叶子令牌（无交集）但档位未开 enforce ⇒ 仍 fail-closed，只记观测。

    - `shadow` / 缺省（env 未设）：记 `seal_degrade_shadow_count`（观测期
      判据：满 3 轮真实项目零反例才许切 enforce）；
    - `off`：应急回滚闸，两个计数都不动。
    """
    _stuck_state(monkeypatch, layout)
    if mode != "default":
        monkeypatch.setenv(svc.SEAL_READBACK_DEGRADE_ENV, mode)
    policy = _leaf_policy(layout)

    with pytest.raises(svc.SealReadbackError):
        await svc._seal_git_bootstrap_files(policy, svc._AsyncGrant(WriteGrant()))

    snap = telemetry.snapshot()
    assert snap["seal_degraded_count"] == 0
    assert snap["seal_degrade_shadow_count"] == (
        1 if mode in ("shadow", "default") else 0)


def test_seal_readback_mode_parsing(monkeypatch):
    """档位 env 解析：三值各自命中（大小写/空白容忍），未知/空/未设 ⇒ shadow。"""
    for raw, want in (
        ("shadow", "shadow"), ("enforce", "enforce"), ("off", "off"),
        ("  ENFORCE  ", "enforce"), ("Off", "off"),
        ("bogus", "shadow"), ("", "shadow"),
    ):
        monkeypatch.setenv(svc.SEAL_READBACK_DEGRADE_ENV, raw)
        assert svc._seal_readback_mode() == want, raw
    monkeypatch.delenv(svc.SEAL_READBACK_DEGRADE_ENV, raising=False)
    assert svc._seal_readback_mode() == "shadow"


def test_seal_leak_intersects_token_state_predicate(layout):
    """状态判据单测：判的是「令牌 SID 集 ∩ 泄漏 SID」，不读命令文本。

    - 叶子令牌不携带 git_main_sid ⇒ 无交集（降级候选）；
    - MAIN 令牌携带 ⇒ 有交集（任何档位 fail-closed）；
    - 反向对照：泄漏的是叶子令牌**真携带**的 git_sid ⇒ 有交集（不得降级）。
    """
    leaf = _leaf_policy(layout)
    main = _main_policy(layout)
    assert svc._seal_leak_intersects_token(leaf, [layout.main_sid]) is False
    assert svc._seal_leak_intersects_token(main, [layout.main_sid]) is True
    assert svc._seal_leak_intersects_token(
        leaf, [git_sid(layout.proj_real)]) is True


# ══════════════════════════════════════════════════════════════════
# 组 3 —— 类型契约：SealReadbackError 与统一归因通道的接缝
# ══════════════════════════════════════════════════════════════════


def test_seal_readback_error_type_contract():
    """`SealReadbackError` 必须走既有 fail-closed 归因通道。

    `platform_side=True`（构造点亲笔声明：平台自己刚做过 seal，有信息优势
    —— errors.py 标注标准 (b)）⇒ `is_platform_side` 判据为 True ⇒ 下游统一
    盖 `runner_failed` 戳不受影响；`api_name` 为空（这不是某次 Win32 调用的
    失败签名）。
    """
    assert issubclass(svc.SealReadbackError, SandboxUnavailableError)
    exc = svc.SealReadbackError(
        "seal read-back failed: X:/p/.git/config",
        leaking_sids=["S-1-4-9-9", "S-1-4-2-2"], path="X:/p/.git/config")
    assert exc.platform_side is True
    assert exc.api_name == ""
    assert is_platform_side(exc) is True
    # leaking_sids 有序（sorted 契约 —— 裁决与日志的去噪前提）+ seal_path 保真
    assert exc.leaking_sids == ["S-1-4-2-2", "S-1-4-9-9"]
    assert exc.seal_path == "X:/p/.git/config"
