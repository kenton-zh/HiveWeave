"""fixplan #5：失败签名归一化收尾 —— 剥 uuid/时间戳/哈希，路径归一到稳定形态。

旧形态（``signature_of`` 只做空白归一 + 截断）：error 原文里的
``attestation_id`` 等**每次生成**的 uuid、时间戳、含用户名/盘符的绝对路径
全部进签名身份 ⇒ 同类错误记成多条互异签名（TEST_DSH_56 实测 31 条
零重复）⇒ R7（知识共享判据）算出 0 的假阴性 —— 真实的"同一坑被第二人
复踩"在表里看不出来。

验收（fixplan §三 #5）：
① 同类错误、不同 uuid ⇒ 同一签名；② 不同类错误 ⇒ 不同签名；
③ 阳性对照：灌入带 uuid 的两条同类错误 ⇒ 去重后应为 1。
路径归一方向（18:05 采纳并收窄）：**直接项目根相对**（或无 root 时剥到
尾两段），不要"相对→绝对→根相对"三步。
"""

from __future__ import annotations

from hiveweave.services.failure_signature import signature_of

_UUID_A = "3f2b8c1a-9d4e-4f6a-b2c3-1e5d7f9a0b2c"
_UUID_B = "7e1d9f3b-2a6c-4e8d-9b1f-0c4a6e8d2f7a"


def test_same_error_different_uuid_same_signature():
    """验收①：同类错误、不同 uuid ⇒ 同一签名。"""
    e1 = (
        f"submit rejected: attestation {_UUID_A} expired "
        "evidence missing for criterion 1"
    )
    e2 = (
        f"submit rejected: attestation {_UUID_B} expired "
        "evidence missing for criterion 1"
    )
    assert signature_of(e1) == signature_of(e2)


def test_different_errors_different_signatures():
    """验收②：不同类错误 ⇒ 不同签名（剥噪声不得把不同错误洗成同一条）。"""
    e1 = f"git merge failed: conflict in src/app.py ({_UUID_A})"
    e2 = f"bash sandbox denied: out of bounds write ({_UUID_A})"
    assert signature_of(e1) != signature_of(e2)


def test_positive_control_dedup_to_one():
    """验收③（阳性对照）：带 uuid 的两条同类错误灌进集合 ⇒ 去重后为 1。"""
    seen = set()
    for uuid in (_UUID_A, _UUID_B, "a" * 8 + "-aaaa-bbbb-cccc-dddddddddddd"):
        e = f"runner died: spawn {uuid} failed to start child process"
        sig = signature_of(e)
        assert sig is not None
        seen.add(sig)
    assert len(seen) == 1


def test_timestamps_stripped():
    """ISO 时间戳与 13 位毫秒 epoch 都是噪声 ⇒ 剥成 <ts> 后同签名。"""
    e1 = "stream error: 2026-09-15T08:30:01.123Z connection lost mid-run"
    e2 = "stream error: 2026-09-14T22:01:59+08:00 connection lost mid-run"
    e3 = "stream error: 1757900000000 connection lost mid-run"
    s1, s2, s3 = signature_of(e1), signature_of(e2), signature_of(e3)
    assert s1 == s2, (s1, s2)
    assert s1 == s3, (s1, s3)


def test_root_relative_paths():
    """路径归一：传 root ⇒ 项目根前缀归一为 ``.``，跨项目/跨机可对上。"""
    e1 = f"D:\\work\\proj1\\src\\app.py raised RuntimeError ({_UUID_A}) boom"
    e2 = f"D:\\work\\proj2\\src\\app.py raised RuntimeError ({_UUID_B}) boom"
    assert signature_of(e1, root="D:\\work\\proj1") == signature_of(
        e2, root="D:\\work\\proj2"
    )
    # 大小写不敏感（Windows 盘符/路径大小写随意写）
    assert signature_of(e1, root="d:\\WORK\\PROJ1") == signature_of(
        e1, root="D:\\work\\proj1"
    )


def test_absolute_paths_stripped_to_tail_without_root():
    """无 root ⇒ 绝对路径剥到尾两段（消灭盘符与用户名，保留文件名信息）。"""
    e1 = f"C:\\Users\\alice\\proj\\src\\app.py: module not found {_UUID_A}"
    e2 = f"C:\\Users\\bob\\elsewhere\\src\\app.py: module not found {_UUID_B}"
    assert signature_of(e1) == signature_of(e2)


def test_unix_paths_stripped():
    e1 = "/home/alice/proj/src/app.py: permission denied while writing"
    e2 = "/home/bob/other/src/app.py: permission denied while writing"
    assert signature_of(e1) == signature_of(e2)


def test_plain_error_unchanged_shape():
    """回归：不含噪声的普通错误只做空白归一（与旧行为等价，不丢信息）。"""
    e = "Error: unsupported dialect for pwsh runner, command skipped entirely"
    assert signature_of(e) == (
        "Error: unsupported dialect for pwsh runner, command skipped entirely"
    )


def test_root_prefix_does_not_eat_sibling_dir():
    """root 前缀替换有词边界：``proj1`` 不得匹配 ``proj1-archive`` ——
    兄弟目录的错误不得被归并成本体目录的签名。"""
    own = signature_of(
        "D:\\work\\proj1\\src\\app.py raised RuntimeError boom",
        root="D:\\work\\proj1",
    )
    sibling = signature_of(
        "D:\\work\\proj1-archive\\src\\app.py raised RuntimeError boom",
        root="D:\\work\\proj1",
    )
    assert own is not None and ".\\" in own
    assert sibling is not None and "app.py" in sibling
    assert own != sibling  # 兄弟目录 ≠ 本体目录（不归并）


def test_too_short_after_stripping_returns_none():
    """剥完噪声后信息量不足 ⇒ None（不广播），与旧行为一致。"""
    assert signature_of(f"dead ({_UUID_A})") is None


def test_git_sha40_not_eaten_by_hex32_rule():
    """40 位 git sha 是**内容稳定**的标识，不是噪声 —— 不得被 32 位规则咬掉。

    性质断言（不是恒真的自等）：① sha 完整存活在签名里；② 两个不同
    sha 必须给出不同签名（内容稳定标识不归并）。"""
    sha1 = "a1b2c3d4" * 5  # 40 hex chars
    e1 = f"merge landed at {sha1} with conflict markers present in file"
    s1 = signature_of(e1)
    assert s1 is not None and sha1 in s1
    sha2 = "c3d4e5f6" * 5
    s2 = signature_of(
        f"merge landed at {sha2} with conflict markers present in file"
    )
    assert s2 is not None and sha2 in s2
    assert s1 != s2
