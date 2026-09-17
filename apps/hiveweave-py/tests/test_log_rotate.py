"""日志轮转（09-17 EXE 运维缺口）：``data/logs/`` 不能无限增长。

实测背景：``dist/HiveWeave/data/logs/`` 163 MB —— ``server.out.log`` 95.1 MB
+ ``launcher.out.log`` 72.3 MB，两个都是纯 append、无上限。EXE 用户长期挂着
平台 ⇒ 单文件只增不减。

本文件的验收重点**不是**"代码里有个 rotate 函数"，而是：
1. 阈值**真的**能触发翻转（造大日志，断言 ``.1`` 出现且旧内容一字不丢）；
2. 翻转后**继续写还落在主文件**（句柄重开生效 —— stale fd 是最容易漏的半场）；
3. ``_FlushFile`` 这个**真实消费者**（main.py 的每写一行路径）确实接线。

⚠ 测试不用文本子串判"有没有轮转"这种模糊口径：判据是**文件是否存在于
磁盘**（``.1`` 有没有、主文件字节数在阈值内），即状态判据。
"""

from __future__ import annotations

import pytest

from hiveweave import main as hw_main
from hiveweave.util import log_rotate


@pytest.fixture(autouse=True)
def _tiny_threshold(monkeypatch):
    """把阈值压到 1 KiB，免得测试为了触发翻转真去写 8 MiB。"""
    monkeypatch.setenv("HIVEWEAVE_LOG_MAX_BYTES", "1024")


def _line(n: int = 1) -> str:
    return "x" * 100 + f" {n}\n"


# --- SizeRotator 本体 -------------------------------------------------------


def test_no_rotation_below_threshold(tmp_path):
    log = tmp_path / "a.log"
    log.write_text(_line(), encoding="utf-8")
    rotator = log_rotate.make_rotator(log)
    assert rotator is not None
    assert rotator.rotate_if_needed() is None
    assert not (tmp_path / "a.log.1").exists()


def test_rotation_seals_generation_to_backup(tmp_path):
    """翻转＝把当代**整体封存**到 ``.1``，主路径**原地清空**。

    关键判据（三条，缺一不算成功轮转）：
    - 主路径**依然存在**（后续写入必须继续落这里）；
    - 主路径大小为 0（新的一代从这里开始）；
    - ``.1`` 一字不差地拿到了旧内容（不是"大概搬过去了"）。
    """
    log = tmp_path / "a.log"
    old = "".join(_line(i) for i in range(40))  # 4 KB > 1 KiB
    log.write_text(old, encoding="utf-8")
    rotator = log_rotate.make_rotator(log)
    assert rotator is not None

    rotated = rotator.rotate_if_needed()

    assert rotated == log.with_name("a.log.1"), "返回值必须是 .1 路径（回调信号）"
    assert log.exists(), "轮转后主路径必须存在（后续写入要继续落这里）"
    assert log.stat().st_size == 0, "主文件必须重置为空"
    assert (tmp_path / "a.log.1").read_text(encoding="utf-8") == old, "旧内容一字不丢"
    assert not log.read_bytes().startswith(b"\x00"), "主文件不得留尾零/NUL 空洞"


def test_second_rotation_moves_previous_generation_along(tmp_path):
    """第二次翻转把上一代推到 ``.2`` —— 链是**顺移**不是覆盖。"""
    log = tmp_path / "a.log"
    rotator = log_rotate.SizeRotator(log, max_bytes=1024, backup_count=3)
    for gen in range(5):
        log.write_text(f"GEN{gen}\n" + "Q" * 4000, encoding="utf-8")
        assert rotator.rotate_if_needed() is not None

    # 最近三代依次是 GEN4 / GEN3 / GEN2（GEN1/GEN0 被挤掉了）
    for suffix, gen in ((".1", 4), (".2", 3), (".3", 2)):
        got = log.with_name(f"a.log{suffix}").read_text(encoding="utf-8")
        assert got.startswith(f"GEN{gen}"), f"a.log{suffix} 应是 GEN{gen}，实得 {got[:12]!r}"
    assert not log.with_name("a.log.4").exists(), "backup_count=3 ⇒ 不得有 .4"


def test_backup_count_one_keeps_single_generation(tmp_path):
    """``backup_count=1``：``.1`` 是"上一代"，再翻一次被新一代覆盖。"""
    log = tmp_path / "a.log"
    rotator = log_rotate.SizeRotator(log, max_bytes=1024, backup_count=1)
    log.write_text("OLDGEN\n" + "P" * 4000, encoding="utf-8")
    assert rotator.rotate_if_needed() is not None
    assert log.with_name("a.log.1").read_text(encoding="utf-8").startswith("OLDGEN")

    log.write_text("NEWGEN\n" + "P" * 4000, encoding="utf-8")
    assert rotator.rotate_if_needed() is not None
    assert log.with_name("a.log.1").read_text(encoding="utf-8").startswith("NEWGEN")
    assert not log.with_name("a.log.2").exists()


def test_make_rotator_accepts_empty_path():
    assert log_rotate.make_rotator(None) is None
    assert log_rotate.make_rotator("") is None


def test_rotator_returns_none_for_missing_file(tmp_path):
    rotator = log_rotate.make_rotator(tmp_path / "never-existed.log")
    assert rotator is not None
    assert rotator.rotate_if_needed() is None  # 不存在不是错误，只是没得转


def test_rotation_leaves_no_nul_hole_after_reopen(tmp_path):
    """真实场景：句柄在轮转期间一直持着，翻转后必须能继续落盘且不留空洞。

    这是本模块最容易漏的半场 —— ``os.replace`` 挪走了 inode，但**持有句柄的
    进程**会继续往那个已被搬到 ``.1`` 的 inode 写。所以调用方必须重开句柄；
    重开漏了的话新内容会进 ``.1``，主文件变成一个空洞文件（读起来全是 NUL）。
    """
    log = tmp_path / "a.log"
    rotator = log_rotate.SizeRotator(log, max_bytes=512, backup_count=1)
    with open(log, "a", encoding="utf-8", buffering=1) as fh:
        fh.write("PRE-ROTATE\n" * 60)  # 720 B > 512
        assert rotator.rotate_if_needed() is not None
        # 模拟调用方：翻转后重开句柄再写
        with open(log, "a", encoding="utf-8", buffering=1) as fh2:
            fh2.write("POST-ROTATE\n")

    assert log.read_text(encoding="utf-8") == "POST-ROTATE\n", "主文件只应有翻转后的新内容"
    assert "PRE-ROTATE" in log.with_name("a.log.1").read_text(encoding="utf-8")


def test_make_rotator_falls_back_on_garbage_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVEWEAVE_LOG_MAX_BYTES", "not-a-number")
    rotator = log_rotate.make_rotator(tmp_path / "a.log")
    assert rotator is not None
    assert rotator.max_bytes == log_rotate.DEFAULT_MAX_BYTES


def test_reset_offset_moves_handle_to_end(tmp_path):
    """翻转后把常驻句柄的偏移归零 —— 不归零就会在旧偏移处续写留空洞。"""
    log = tmp_path / "a.log"
    rotator = log_rotate.SizeRotator(log, max_bytes=64, backup_count=1)
    handle = open(log, "a", encoding="utf-8", buffering=1)
    try:
        handle.write("Z" * 200)
        handle.flush()
        assert rotator.rotate_if_needed() is not None
        assert log.stat().st_size == 0, "翻转后主文件已清零"
        assert rotator.reset_offset(handle) is True
        assert handle.tell() == 0, "偏移必须归零，否则下一次写会留 NUL 空洞"
        handle.write("after\n")
        handle.flush()
    finally:
        handle.close()

    raw = log.read_bytes()
    # 文本模式下 Windows 会把 "\n" 落成 "\r\n"，故按解码后的逻辑内容断言，
    # 不用字面字节比较（那是把平台换行符当成契约）。
    assert log.read_text(encoding="utf-8") == "after\n", "主文件应只含翻转后的新内容"
    assert b"\x00" not in raw, "不得有 NUL 空洞"


def test_reset_offset_tolerates_handles_without_seek(tmp_path):
    """没有 seek 的对象（如 Tee 之类代理）不得把轮转带崩，返回 False。"""
    log = tmp_path / "a.log"
    rotator = log_rotate.SizeRotator(log, max_bytes=64, backup_count=1)

    class _NoSeek:
        pass

    assert rotator.reset_offset(_NoSeek()) is False
    assert rotator.reset_offset(object()) is False


def test_reset_offset_swallows_raising_handle(tmp_path):
    """``seek`` 自身抛异常（已关文件/坏管道）必须被吃掉并返回 False。

    契约是"轮转绝不把进程带走"——若这里漏抛，日志设施坏了会连带进程崩。
    """
    log = tmp_path / "a.log"
    rotator = log_rotate.SizeRotator(log, max_bytes=64, backup_count=1)

    class _Raising:
        def seek(self, *a, **k):
            raise OSError(22, "handle already closed")

    class _RaisingValue:
        def seek(self, *a, **k):
            raise ValueError("I/O operation on closed file")

    assert rotator.reset_offset(_Raising()) is False
    assert rotator.reset_offset(_RaisingValue()) is False


def test_positive_control_rotation_actually_happens(tmp_path):
    """阳性对照：若轮转被"静默关掉"，本用例必须转红。

    存在的理由：其余用例都在断言"翻转后的状态"，一旦翻转**从不发生**，
    有些断言（如"主文件大小 == 0"在文件本就为空时）可能仍然绿。这里直接
    钉住"翻转**发生过**"这个事实本身——返回值非 None + ``.1`` 真在场。
    """
    log = tmp_path / "a.log"
    rotator = log_rotate.SizeRotator(log, max_bytes=128, backup_count=1)
    log.write_text("PAYLOAD\n" + "K" * 4096, encoding="utf-8")

    rotated = rotator.rotate_if_needed()

    assert rotated is not None, "超阈值必须返回非 None（= 已翻转）"
    assert rotated.exists(), "返回的备份路径必须真实存在（不是空话）"
    assert rotated.stat().st_size > 0, "备份不得为空文件"
    assert "PAYLOAD" in rotated.read_text(encoding="utf-8"), "内容必须真的被搬走"


# --- _FlushFile 真实接线（main.py 的每写一行路径）----------------------------


def test_flush_file_rotates_while_writing(tmp_path, monkeypatch):
    """真实消费者：连写 200 行必须触发翻转，且翻转后继续写落主文件。"""
    monkeypatch.setenv("HIVEWEAVE_LOG_MAX_BYTES", "4096")
    log = tmp_path / "server.out.log"
    stream = hw_main._FlushFile(log)
    try:
        for i in range(200):
            stream.write(f"{i:04d} " + "z" * 120 + "\n")
        stream.flush()
    finally:
        stream.close()

    backup = tmp_path / "server.out.log.1"
    assert backup.exists(), "连写 200 行 ×130B = 26KB 远超 4KiB，必须已轮转"
    assert backup.stat().st_size >= 4096, "封存代必须留下足量历史"
    # 无空洞：文件以 NUL 开头说明 stale fd 被续写进了旧偏移（未重开句柄）
    assert not log.read_bytes().startswith(b"\x00"), "轮转后必须重开句柄，不能留空洞"
    # 稳态上限：第二代的量级，绝不能等于"没轮转"的 26 KB
    assert log.stat().st_size < 26_000, "主文件必须真的被重置（不是只搬了家）"


def test_flush_file_write_returns_len_and_keeps_content(tmp_path, monkeypatch):
    """翻转不得吃掉写入返回值，也不得丢内容（既有 M10 契约不许回退）。"""
    monkeypatch.setenv("HIVEWEAVE_LOG_MAX_BYTES", "1024")
    log = tmp_path / "server.out.log"
    stream = hw_main._FlushFile(log)
    try:
        assert stream.write("hello\n") == 6
    finally:
        stream.close()
    assert "hello" in log.read_text(encoding="utf-8")


def test_flush_file_survives_unrotatable_path(tmp_path, monkeypatch):
    """轮转器构造失败 ⇒ 退化为纯 append，**不得**把日志写坏或抛异常。"""
    monkeypatch.setattr(hw_main, "make_rotator", lambda _p: None)
    log = tmp_path / "server.out.log"
    stream = hw_main._FlushFile(log)
    try:
        for i in range(50):
            stream.write(f"line-{i}\n")
    finally:
        stream.close()
    content = log.read_text(encoding="utf-8")
    assert "line-0" in content and "line-49" in content


# --- launcher._RotatingStdout（EXE 的 stdout 出口）--------------------------
# launcher.py 是 apps/desktop 下的独立脚本（不在 src 包内），导入前需把该目录
# 加进 sys.path；import 失败就跳过 —— 让"launcher 侧代理未接线"退化成**可见的
# skip**，而不是让测试静默少跑半场（模块级 importorskip 会连上面 10 个用例一起吞掉，
# 那是"看似有守卫"的典型）。

import importlib.util
import sys

_LAUNCHER_PATH = (
    hw_main.__file__.replace("\\", "/").split("/hiveweave-py/")[0]
    + "/desktop/launcher.py"
)


def _load_launcher():
    spec = importlib.util.spec_from_file_location("_hw_launcher", _LAUNCHER_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_hw_launcher"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("_hw_launcher", None)
        return None
    return module


_launcher = _load_launcher()


def test_rotating_stdout_rotates(tmp_path, monkeypatch):
    if _launcher is None:
        pytest.skip(f"launcher 不可导入：{_LAUNCHER_PATH}")
    monkeypatch.setenv("HIVEWEAVE_LOG_MAX_BYTES", "1024")
    monkeypatch.setattr(_launcher, "_OUT_ROTATE_EVERY_LINES", 4)
    log = tmp_path / "launcher.out.log"
    rotator = _launcher._make_stdout_rotator(log)
    assert rotator is not None
    proxy = _launcher._RotatingStdout(
        open(log, "a", encoding="utf-8", buffering=1), rotator
    )
    for i in range(200):
        proxy.write(f"{i:04d}" + "w" * 100 + "\n")

    assert (tmp_path / "launcher.out.log.1").exists(), "代理必须触发翻转"
    assert log.stat().st_size < 20_000, "主文件必须被重置（不是只搬了家）"
    assert not log.read_bytes().startswith(b"\x00"), "翻转后必须换掉内部句柄"


def test_rotating_stdout_without_rotator_is_inert(tmp_path):
    """rotator=None（hiveweave 不可导入的降级）不得抛异常，只是不轮转。"""
    if _launcher is None:
        pytest.skip(f"launcher 不可导入：{_LAUNCHER_PATH}")
    log = tmp_path / "launcher.out.log"
    proxy = _launcher._RotatingStdout(
        open(log, "a", encoding="utf-8", buffering=1), None
    )
    for i in range(300):
        proxy.write(f"line-{i}\n")
    assert log.stat().st_size > 1024
    assert not (tmp_path / "launcher.out.log.1").exists()


# --- 采样与兜底的关系（2026-09-17 审计必修的守卫）---------------------------
#
# 背景：`_TeeStream` 写完每个 stream 都会调 `flush()`。早先 `flush` 以
# `force=True` 绕过采样计数器 ⇒ 每行一次 `stat()`，采样全废。
# 修的时候**极易过头**成另一个坑：让 `flush` "只在余量够时才检查" ——
# 那是同义反复（`write` 达到窗口就归零，所以"余量没够"是常态），
# 兜底恒不触发。本轮首版即栽在此，被下面第 ② 条测试的同类场景抓出。


def test_sampling_actually_amortizes_stat_calls(tmp_path, monkeypatch):
    """① 采样真的摊销了 `stat()`：**经真实装配**（`_TeeStream`）连写 N 行。

    ⚠⚠ 2026-09-17 **第二轮审计必修**：本用例原先**只调 `stream.write()`、
    不套 `_TeeStream`** ⇒ 它守的是"write 路自己摊销"，而缺陷正长在
    `write` 与 `flush` **两个出口之间**（tee 逐行 flush 把兜底路抬成热路径）
    ⇒ 守卫**绕过了它自己声明要防的那条路**，永远抓不到。

    审计原话：「并把这个守卫改成套 `_TeeStream` 驱动，否则永远守不住」。
    故现在按 `_configure_logging` 的**同一装配**（`_TeeStream(stdout, flushfile)`）
    驱动；阳性对照：把 `_TeeStream.write` 里的 `flush` 加回去 ⇒ 调用数
    约翻倍（实测 1003 vs ~4），本用例转红。
    """
    monkeypatch.setenv("HIVEWEAVE_LOG_MAX_BYTES", "1000000")  # 大到不翻转
    log = tmp_path / "server.out.log"
    stream = hw_main._FlushFile(log)
    calls = {"n": 0}
    real = stream._do_rotate

    def _spy():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(stream, "_do_rotate", _spy)

    class _Sink:
        """替掉 `sys.stdout`：只吞字节，绝不触发额外 flush。"""

        def write(self, s):
            return len(s)

    tee = hw_main._TeeStream(_Sink(), stream)
    try:
        n_lines = 1000
        for i in range(n_lines):
            tee.write(f"{i}\n")          # ← 真实路径：经 tee
    finally:
        stream.close()

    window = hw_main._ROTATE_CHECK_EVERY_LINES
    assert calls["n"] <= n_lines // window + 1, (
        f"经 `_TeeStream` 写 1000 行只应做 ~{n_lines // window} 次预检"
        f"（采样窗口 {window}），实际 {calls['n']} 次 —— 采样未生效。"
        f"⚠ 最常见原因：`_TeeStream.write` 又对每个 stream 逐行调了 `flush()`，"
        f"把 `flush` 的兜底路抬成与 write 同级的**热路径**（实测 1003 次）。"
    )


def test_flush_is_a_real_fallback_not_a_tautology(tmp_path, monkeypatch):
    """② `flush()` 必须是**真兜底**：写入量凑不满采样窗口时也能翻转。

    这是"同义反复"那个坑的守卫 —— 若 `flush` 改回"只在余量够时才检查"，
    本条转红（200 行 < 窗口 256 ⇒ 永远不检查 ⇒ 永不翻转）。
    """
    monkeypatch.setenv("HIVEWEAVE_LOG_MAX_BYTES", "4096")
    log = tmp_path / "server.out.log"
    stream = hw_main._FlushFile(log)
    window = hw_main._ROTATE_CHECK_EVERY_LINES
    n_lines = window - 56          # 刻意**少于**一个采样窗口
    assert n_lines > 0
    try:
        for i in range(n_lines):
            stream.write(f"{i:04d} " + "z" * 120 + "\n")   # 每行 130B
        stream.flush()             # ← 兜底必须在这里生效
    finally:
        stream.close()

    total = n_lines * 130
    assert total > 4096, "用例前提：总字节确实超过阈值"
    assert (tmp_path / "server.out.log.1").exists(), (
        f"写满 {total}B（超阈值 4096）却未翻转 —— `flush` 的兜底没生效。"
        f"⚠ 若改成「只在 _pending_lines 够时才检查」就会退化成这条（同义反复："
        f"{n_lines} < 窗口 {window} ⇒ 永远不检查）"
    )
