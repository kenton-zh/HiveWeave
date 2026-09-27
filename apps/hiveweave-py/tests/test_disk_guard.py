"""I1 / 报告 P0-0：卷余量护栏（`services/disk_guard.py` + spawn 漏斗接入）。

被审事故：叶子 agent 的 `unittest discover` 把宿主卷写掉 188 GB 到 ENOSPC
⇒ 平台自身 92 分钟写不进库。本测试锁住三件事：

1. **预检**：余量低于下限 ⇒ 拒跑（带处方），且拒因必须与「没有这个程序」分档；
2. **运行中**：低于危险线 ⇒ 杀**进程树**；
3. **三个正交事实位**：`bytes_written` / `hit_limit` / `tree_reaped` 各自独立
   —— 「写了多少」不等于「是否触顶」，「触顶了」不等于「树已回收」。

⚠ 本文件包含**阳性对照**（`test_positive_control_*`）：把触发条件改坏 ⇒ 必须
不触发（证明「能触发」不是因为断言恒真）。只跑正确路径不算验。
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from hiveweave.services import disk_guard as dg
from hiveweave.util import win_subprocess as ws

#: 大到任何真实卷都不可能满足 ⇒ 必然触发预检拒绝 / 运行中触顶。
IMPOSSIBLE_FREE = 10**18


@pytest.fixture(autouse=True)
def _clean_guard():
    dg.reset_for_tests()
    yield
    dg.reset_for_tests()


def _spawn_sleeper(seconds: float = 30.0) -> subprocess.Popen:
    """裸 Popen 起一个长睡进程（测试文件不在 spawn 漏斗守卫的扫描面内）。

    用它而不是 `hidden_popen`：本测试要**自己控制**危险线与轮询间隔，
    而 `hidden_popen` 会用生产默认值（1 GiB / 2 s）挂一次会话。
    """
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


# ── 卷解析与读数 ────────────────────────────────────────────


def test_volume_root_is_anchored(tmp_path):
    root = dg.volume_root(tmp_path)
    assert root.endswith(":\\") or root == "/", f"非卷根形态: {root!r}"


def test_read_volume_reports_total(tmp_path):
    reading = dg.read_volume(tmp_path)
    assert reading.total > 0
    assert reading.free >= 0


# ── ① 预检 ──────────────────────────────────────────────────


def test_precheck_allows_when_plenty_of_space(tmp_path):
    allowed, reading, remedy = dg.precheck(tmp_path)
    assert allowed is True
    assert remedy == ""
    assert reading is not None


def test_precheck_refuses_below_floor(tmp_path):
    allowed, reading, remedy = dg.precheck(tmp_path, min_free_bytes=IMPOSSIBLE_FREE)
    assert allowed is False, "余量不可能满足下限时必须拒跑"
    assert reading is not None
    # 处方（拒绝必带处方 —— 本仓纪律）
    assert "GiB" in remedy and "workspacePath" in remedy


def test_precheck_fails_open_when_unreadable(monkeypatch):
    """量不出余量 ⇒ **放行**（护栏不得变成新的失败源：那会把一个 P0 换成另一个）。"""

    def _boom(_path):
        raise OSError("device not ready")

    monkeypatch.setattr(dg, "read_volume", _boom)
    allowed, reading, remedy = dg.precheck("D:/whatever")
    assert allowed is True
    assert reading is None
    assert remedy == ""


# ── ③ 事实位（无触顶的正常路径）──────────────────────────────


def test_watch_and_finish_record_three_orthogonal_facts(tmp_path):
    proc = _spawn_sleeper()
    try:
        facts = dg.watch(proc, tmp_path, danger_free_bytes=0)  # 危险线 0 ⇒ 不触顶
        assert dg.active_sessions() == 1
        time.sleep(0.3)
        done = dg.finish(proc)
        assert done is facts, "finish 必须返回挂在 proc 上的同一个事实对象"
        assert facts.hit_limit is False, "观测了整段且没触发 ⇒ 必须是 False（不是 None）"
        assert facts.tree_reaped is None, "没 kill ⇒ 树回收是「未观测」，不是 True"
        assert facts.bytes_written is not None, "起止读数都在 ⇒ 必须有字节量级"
        assert dg.active_sessions() == 0, "finish 必须注销会话（否则会话表泄漏）"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_finish_is_idempotent(tmp_path):
    proc = _spawn_sleeper()
    try:
        dg.watch(proc, tmp_path, danger_free_bytes=0)
        first = dg.finish(proc)
        second = dg.finish(proc)  # watcher 可能已自动收尾 ⇒ 必须安全
        assert first is not None and second is first
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ── ② 触顶：杀树 + 三正交位 ─────────────────────────────────


def test_trip_kills_tree_and_sets_all_three_facts(tmp_path):
    proc = _spawn_sleeper(60)
    try:
        facts = dg.watch(
            proc, tmp_path, danger_free_bytes=IMPOSSIBLE_FREE, interval_s=0.05
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not facts.hit_limit:
            time.sleep(0.05)
        assert facts.hit_limit is True, "危险线不可满足时必须触顶"
        assert facts.free_at_trip is not None, "触顶必须留当时的读数"
        assert facts.trip_at_ms is not None
        # 进程必须真被杀掉（状态判据：poll() 不再返回 None）
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.1)
        assert proc.poll() is not None, "触顶后进程必须终止"
        assert facts.tree_reaped is True, "进程确实退出后必须报已回收"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_positive_control_unreachable_danger_line_never_trips(tmp_path):
    """阳性对照：把触发条件改坏（危险线 0）⇒ **不得**触顶、进程必须活着。

    没有这一条，「触顶测试通过」无法区分「闸灵敏」与「断言恒真」。
    """
    proc = _spawn_sleeper(30)
    try:
        facts = dg.watch(proc, tmp_path, danger_free_bytes=0, interval_s=0.05)
        # ⚠ 必须断言「监控真的挂上了」（2026-09-27 审计 ②-4）：只断言「不触发」
        # 的话，`watch` 因读卷失败提前返回（`watched=False`、**根本没登记会话**）
        # 也会让本对照通过 ⇒ 一条无信息量的假对照。
        assert facts.watched is True, "监控必须真的挂上，否则本对照恒绿"
        assert dg.active_sessions() == 1, "必须真的登记了会话"
        time.sleep(0.5)
        assert facts.hit_limit is None or facts.hit_limit is False
        assert facts.tree_reaped is None
        assert proc.poll() is None, "不该被杀"
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ── 拒因翻译 ────────────────────────────────────────────────


def test_denial_flags_only_for_disk_pressure():
    """⚠ 只给 `denied_by`，**不给** `blocked_by_environment`（2026-09-27 审计 ①-5）。

    该列的定义是「拒绝来自环境（ACL/封条）**而非 runner 没跑起来**」，实现判据是
    `exit_code is not None`（进程**确实跑过**）；而卷预检拒绝恰恰是「**从未启动**」
    ⇒ 标 1 会把该列设计用来区分的两种情形**又合并**（P0-2「同一标志两个含义」的复发）。
    """
    assert dg.denial_flags(dg.DiskPressureError("no space")) == {
        "denied_by": "disk_pressure"
    }
    assert dg.denial_flags(OSError("not found")) == {}
    assert dg.denial_flags(RuntimeError("x")) == {}


def test_denied_by_enum_contains_disk_pressure():
    from hiveweave.tools.result import DENIED_BY_KINDS

    assert "disk_pressure" in DENIED_BY_KINDS


# ── spawn 漏斗接入 ──────────────────────────────────────────


def test_pop_disk_guard_removes_internal_kwarg():
    kwargs = {"cwd": "x", "_disk_guard": False}
    assert ws._pop_disk_guard(kwargs) is False
    assert "_disk_guard" not in kwargs, "内部开关绝不能透传给 subprocess"
    assert ws._pop_disk_guard({}) is True, "默认必须开启"


def test_funnel_precheck_refuses_and_process_never_starts(monkeypatch, tmp_path):
    """预检拒绝 ⇒ `DiskPressureError`，且**进程从未启动**。"""
    from hiveweave.services import disk_guard

    monkeypatch.setattr(
        disk_guard,
        "precheck",
        lambda *_a, **_k: (False, None, "卷 D:\\ 可用空间 0.1 GiB，低于预检下限 4.0 GiB"),
    )
    with pytest.raises(disk_guard.DiskPressureError):
        ws.hidden_popen([sys.executable, "-c", "print(1)"], cwd=str(tmp_path))


def test_funnel_watch_attached_when_process_starts(tmp_path):
    """正常路径：经 `hidden_popen` 起来后，proc 上必须挂着事实对象。"""
    proc = ws.hidden_popen(
        [sys.executable, "-c", "import time; time.sleep(5)"], cwd=str(tmp_path)
    )
    try:
        facts = dg.facts_of(proc)
        assert facts is not None, "漏斗必须给每条子进程挂上卷事实"
        assert facts.watched is True
    finally:
        proc.kill()
        proc.wait(timeout=10)
        dg.finish(proc)
