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

import re

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


# ══════════════════════════════════════════════════════════════════
# 0-4（2026-09-16）：再剥两类「平台自产标识形状」
# ══════════════════════════════════════════════════════════════════
#
# **为什么还要改**：`468bf79` 之后同一根因跨 agent **仍被判成不同身份**。
# 用真实库全量重放（`scripts/replay_failure_signatures.py`，只读）实测：
# 58 项目 144 条失败 → 85 个签名，其中 **3 组同根因被拆开**，现场是 —
#   · `命令指向 worktree A075/A076/A077（不是你所在的树）`
#   · `merging main into hw/A074|A076|A077/work would conflict`
#   · `.hiveweave/reports/<8 位 id>`
# 这三处里的 agent 短号与 8 位十六进制**都是平台自己生成的标识**，与根因正交。
#
# ⚠ **库里「50 行 → 50 签名 ⇒ 0% 去重」这个结论是测量方法错**：
# `_SIGNATURE_MAX_ROWS = 50` 是**裁剪上限**，upsert 去重之后行数恒 ≤50
# ⇒ 拿行数当去重率必然读出 0%。本文件下面的用例是**逐条构造**的判据，
# 不依赖库内行数。

from hiveweave.services.failure_signature import (  # noqa: E402
    make_module_id,
    signature_of as _sig,
)


def test_agent_short_id_normalized_across_agents():
    """★ DoD 验收现场之一：`worktree A075/A076/A077` ⇒ 同一签名。

    这是"同一根因跨 agent"的最直观形态 —— 三个人各自撞到「路径指向别人的
    worktree」，差别只有自己的短号。
    """
    base = (
        "Error: Command blocked: 命令指向 worktree {sid}（不是你所在的树）。"
        "该路径指向**别的 worktree**（``.hiveweave/worktrees/<非本树 id>/…``），"
        "已越出你的授权树根"
    )
    sigs = {_sig(base.format(sid=s)) for s in ("A074", "A075", "A076", "A077")}
    assert len(sigs) == 1, sigs
    assert "<agent>" in (sigs.pop() or "")


def test_branch_short_id_normalized_across_agents():
    """★ DoD 验收现场之二：`hw/A074|A076|A077/work` ⇒ 同一签名。"""
    base = (
        "Sync refused: merging main into hw/{sid}/work would conflict: "
        "index.html. Nothing was merged and nothing was changed"
    )
    sigs = {_sig(base.format(sid=s)) for s in ("A074", "A076", "A077")}
    assert len(sigs) == 1, sigs


def test_task_id8_normalized():
    """★ DoD 验收现场之三：`.hiveweave/reports/<8 位 id>` ⇒ 同一签名。"""
    base = "Error: Directory not found: .hiveweave/reports/{tid}. Not in this tree."
    sigs = {
        _sig(base.format(tid=t))
        for t in ("b8027383", "1027f091", "1dd78a1f", "91765492")
    }
    assert len(sigs) == 1, sigs

    # module_id 是签名的纯函数 ⇒ 它自然也必须一致（DoD 的措辞就是 module_id）
    one = _sig(base.format(tid="b8027383"))
    other = _sig(base.format(tid="1027f091"))
    assert make_module_id("p1", one or "") == make_module_id("p1", other or "")


def test_agent_short_id_shape_not_over_eaten():
    """反向对照：只有**恰好** `A`+3 位数字、且左右不是字母数字才替。

    放宽成子串匹配会把 `XA023`、`A0234`（更长的号）一起吃掉 ——
    那是把不同根因洗成同一条，方向与上面几条相反。
    """
    e_long = "tool XA023A and A0234 are both real identifiers in this message body"
    sig = _sig(e_long)
    assert sig is not None and "A0234" in sig, sig


def test_task_id8_does_not_eat_longer_or_numeric_quantities():
    """反向对照：**恰好 8 位**十六进制才替。

    - 6 位 / 9 位的十进制量（字节数、计数）不得被替 —— 那是内容；
    - 40 位 git sha 由 `test_git_sha40_not_eaten_by_hex32_rule` 钉住；
    - 32 位哈希先被 `<hash>` 吃掉，不会退化成 `<id8>`。
    """
    sig = _sig(
        "archive failed: wrote 327746 bytes into 123456789 slots, "
        "expected 327746 bytes in total"
    )
    assert sig is not None
    assert "327746" in sig and "123456789" in sig, sig

    sig32 = _sig("blob mismatch for " + "a1b2c3d4" * 4 + " during verification step")
    assert sig32 is not None and "<hash>" in sig32 and "<id8>" not in sig32


def test_distinct_root_causes_still_split():
    """★ 反向对照（防过度合并）：措辞不同 = 根因不同 ⇒ 必须不同签名。

    本批只剥**平台自产标识形状**，措辞一行不剥。若有人为了"提高去重率"
    改成按 tool 或按错误类别归并，这条会转红 —— 那是把共享条目变成大杂烩，
    本仓明写「错解比无解更贵」。
    """
    a = _sig("Error: File not found: docs/a.md. Not in this tree.")
    b = _sig("Error: File not found: docs/b.md. Not in this tree.")
    c = _sig("Error: Permission denied: docs/a.md. Not in this tree.")
    assert a and b and c
    assert a != b, "不同文件名被合并 —— 过度归一化"
    assert a != c, "不同错误类别被合并 —— 过度归一化"


def test_signature_stable_for_same_input_and_root():
    """写侧/查侧同参必须同结果；且 **root 参与身份**（带 root 与不带可不同）。
    ⚠ 后半句只对**含项目根前缀的绝对路径**成立 —— 相对路径（`.hiveweave/...`）
    不受 root 影响，两种调用本就同签名。别把它写成"带 root 必不同"。
    """
    e = "Error: Directory not found: .hiveweave/reports/b8027383. Not in this tree."
    root = "D:\\work\\proj"
    assert _sig(e, root=root) == _sig(e, root=root)

    abs_err = "failed to read D:\\work\\proj\\src\\a.py while scanning"
    assert _sig(abs_err, root=root) == _sig(abs_err, root=root)
    assert _sig(abs_err, root=root) != _sig(abs_err)


# ── 0-4 审计处置：四条「别让注释宣称一个实测不成立的不变式」 ─────────


def test_absolute_path_with_placeholder_loses_drive_and_user():
    """★ 审计 D1：**含占位符的绝对路径也必须剥到「尾两段」**。

    占位符形态是 `<id8>`/`<agent>`，**内含尖括号**；而 `_WIN_PATH_RE` 的段
    字符类原先排除 `<>` ⇒ 路径在占位符处截断、匹配回退 ⇒ 盘符与用户名段
    **回流**（`D:/Temp/<id8>/out.txt`），把 docstring 宣称的"绝对路径里的
    用户名盘符已剥"变成假陈述。修法是把 `<>` 放进段字符类。
    """
    cases = [
        "failed reading D:\\Temp\\b8027383\\out.txt while scanning",
        "failed reading D:\\Temp\\A075\\out.txt while scanning",
        "failed reading C:\\Users\\99744\\AppData\\Local\\Temp\\b8027383\\cache.json",
        "failed reading D:\\alice\\A075\\index.html during merge",
    ]
    for err in cases:
        sig = _sig(err)
        assert sig is not None, err
        assert not re.search(r"[A-Za-z]:", sig), (err, sig)
        assert "99744" not in sig and "alice" not in sig, (err, sig)
        assert "<id8>" in sig or "<agent>" in sig, (err, sig)


def test_eight_digit_decimal_is_intentionally_eaten():
    """★ 审计 D3：把「代价」写成**判据**而不是注释。

    `_TASK_ID8_RE` 含纯十进制（task id 是 uuid 前 8 位，纯数字合法）⇒
    恰好 8 位的十进制量会被当成 id。这是**有意接受**的代价：
    代价不对称（漏替 task id = 同一根因永久拆开；误替一个 8 位量 = 少一点精度）。
    若有人把规则收窄成"必须含 a-f"，本用例会转红 —— 那正是实测漏掉
    `t-91765492` 的形态。
    """
    sig = _sig("archive wrote expected 32774618 bytes but size check failed")
    assert sig is not None
    assert "<id8>" in sig, sig  # 有意吃掉的代价，写在这里而不是注释里


def test_hex_length_policy_is_explicit():
    """★ 审计 D2：十六进制串的**长度即语义**，这条策略要被钉住而不是被默认。

    - 恰好 8 位 ⇒ 平台 task id（uuid 前 8 位）⇒ 归并（**含 git 的 8 位短 sha**）；
    - 7 位 / 40 位 ⇒ 视为内容稳定标识 ⇒ **不**归并（40 位由既有用例声明）。
    两条策略不同源是有意的：8 位是平台 id 的长度，7/40 位是 git 的形态。
    """
    eight_a = _sig("patch failed to apply at blob abc12345 during rebase step")
    eight_b = _sig("patch failed to apply at blob def67890 during rebase step")
    assert eight_a is not None and eight_b is not None
    assert eight_a == eight_b, "8 位十六进制必须归并（平台 task id 形态）"

    seven_a = _sig("patch failed to apply at rev eabaa7b during rebase step")
    seven_b = _sig("patch failed to apply at rev bd98c74 during rebase step")
    assert seven_a != seven_b, "7 位短 sha 不得归并"


def test_zero_information_signature_is_not_broadcast():
    """★ 审计 D4：**占位符不计信息量**。

    `"A075 A076 A077 A078"` 在归一化后是 31 字符的 `<agent>`×4 ——
    旧口径只看总长会**开始广播**一条零信息条目（原实现对它返回 None）。
    """
    assert _sig("A075 A076 A077 A078") is None
    assert _sig("A075 A076 A077 A078 A079") is None
    # 反向：真内容仍照常广播
    assert _sig("A075 A076 A077 A078 patch apply rejected by merge gate") is not None



# ── #5 批 C（2026-09-18）：应剥未剥的结构化变化量 ──────────────────


def test_worktree_relocation_suffix_is_stripped():
    """worktree 重定位后缀（A075-b）与裸短号（A075）同归 <agent>。

    §8.8 实测重定位发生 10/9 次 —— 后缀不剥会把同根因拆成多条。"""
    e1 = "A075-b failed to apply patch at src/main.py"
    e2 = "A075 failed to apply patch at src/main.py"
    e3 = "A075-c failed to apply patch at src/main.py"
    assert _sig(e1) == _sig(e2) == _sig(e3)


def test_line_numbers_are_stripped():
    """file.py:123 与 file.py:456 是同一根因（行号是易变量）。"""
    e1 = "AssertionError in tests/x.py:123 expected 3 got 4"
    e2 = "AssertionError in tests/x.py:456 expected 3 got 4"
    assert _sig(e1) == _sig(e2)


def test_durations_sizes_and_token_counts_are_stripped():
    """时长/容量/token 数变化不改变身份。"""
    a = "Request took 1.2s and 512 KB, used 345 tokens, then connection refused"
    b = "Request took 8.9s and 2048 KB, used 999 tokens, then connection refused"
    assert _sig(a) == _sig(b)


def test_temp_dir_random_names_are_stripped():
    """pytest 临时目录随机段不改变身份。"""
    e1 = "tmpab12x9ab/test_config.py missing in workspace"
    e2 = "tmpk3lnqm2o/test_config.py missing in workspace"
    assert _sig(e1) == _sig(e2)
    # 驼峰标识符不误吃（审计 LOW）：mkdtemp 形态 = tmp + 恰 8 位小写/数字
    e3 = "tmpDirectory not found in workspace"
    assert "tmpDirectory" in (_sig(e3) or ""), "驼峰标识符被误剥"


def test_wording_still_splits_different_roots():
    """措辞本身仍不剥（批 C 不改变这条边界）——不同根因不同身份。"""
    a = "build failed: 3 tests failed in 1.2s"
    b = "build passed after 3 retries in 1.2s"
    assert _sig(a) != _sig(b)


def test_module_id_includes_tool_name():
    """F9-A：module_id 三元组化 —— 同签名不同工具并存为多行，不再覆盖。

    阳性对照：把 make_module_id 退回不含 tool ⇒ 本测试转红。"""
    from hiveweave.services.failure_signature import make_module_id

    sig = "connection refused to upstream"
    m_bash = make_module_id("p1", sig, "bash")
    m_pwsh = make_module_id("p1", sig, "pwsh")
    assert m_bash != m_pwsh, "不同工具必须并存为两行（存储侧不是并读侧）"
    assert m_bash == make_module_id("p1", sig, "bash"), "同 (project, sig, tool) 稳定"
    assert make_module_id("p1", sig) != make_module_id("p2", sig, "bash"), "跨项目仍隔离"
