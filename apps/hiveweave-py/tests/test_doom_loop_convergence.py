"""R3 P0-1 收敛判据回归：fail_signature_for_round 语义。"""
from __future__ import annotations

import json

from hiveweave.llm.streamer.doom_loop import fail_signature_for_round


def _call(name: str, args: object, cid: str) -> dict:
    return {"name": name, "id": cid, "arguments": args}


def test_signature_ignores_successful_calls():
    calls = [
        _call("read_file", {"filePath": "a.md"}, "ok-tc"),
        _call("bash", {"command": "pytest x.py"}, "err-tc"),
    ]
    sig = fail_signature_for_round(calls, error_ids={"err-tc"})
    # 40 轮修正后：指纹 = (tool_name, 全参 SHA-256[:16])——断言工具名 + 确定性
    assert sig is not None and sig[0] == "bash"
    assert sig == fail_signature_for_round(calls, error_ids={"err-tc"})


def test_same_tool_diff_args_produce_diff_signatures():
    """试错轮（参数在变）指纹必须不同 —— 这是不误杀的判据核心。"""
    e1, e2, e3 = "e1", "e2", "e3"
    s1 = fail_signature_for_round(
        [_call("bash", {"command": "pytest -p no:cacheprovider a.py"}, e1)],
        error_ids={e1},
    )
    s2 = fail_signature_for_round(
        [_call("bash", {"command": "pytest --basetemp=C:\\Temp x.py"}, e2)],
        error_ids={e2},
    )
    s3 = fail_signature_for_round(
        [_call("bash", {"command": "pytest -p no:cacheprovider a.py"}, e3)],
        error_ids={e3},
    )
    assert s1 is not None and s1 != s2      # 参数变 = 不同源
    assert s1 == s3                          # 参数回同 = 同源（原地 T 回来）


def test_signature_none_when_no_failure():
    calls = [_call("bash", {"command": "git status"}, "ok")]
    assert fail_signature_for_round(calls, error_ids=set()) is None


def test_pwsh_prefix_collision_no_longer_same_source():
    """40 轮实测回归：pwsh 诊断链共享 ≥60 字符 JSON 前缀但命令实质不同——
    旧 args[:60] 指纹判同墙误杀合法试错（岚生 file:// 热修连续 early-end
    的根因之一）；全参哈希必须判不同源。

    审计 ④ 修正：公共前缀必须 ≥60 字符，否则旧实现下也判不同源，
    测试守护不了修复。两条命令的 canonical JSON 公共前缀约 76 字符
    （差异点在 "step 0" 之后的 1/2），远超旧指纹窗口。"""
    a = {
        "command": "Write-Host '=== diag-a: boot chain evidence gathering, step 01 ==='; Get-Content index.html | Out-String; git diff --stat"
    }
    b = {
        "command": "Write-Host '=== diag-a: boot chain evidence gathering, step 02 ==='; Get-Content package.json | Out-String; npm --version"
    }
    ca = json.dumps(a, sort_keys=True, ensure_ascii=False)
    cb = json.dumps(b, sort_keys=True, ensure_ascii=False)
    # 守护前提自检：公共前缀必须超过旧指纹窗口（60 字符）
    common = 0
    for x, y in zip(ca, cb):
        if x != y:
            break
        common += 1
    assert common > 60, f"测试样本公共前缀 {common} 字符，未覆盖旧 args[:60] 窗口"
    s1 = fail_signature_for_round([_call("pwsh_main", a, "e1")], error_ids={"e1"})
    s2 = fail_signature_for_round([_call("pwsh_main", b, "e2")], error_ids={"e2"})
    assert s1 is not None and s2 is not None and s1 != s2
    # 真·重试同一命令仍判同源
    s3 = fail_signature_for_round([_call("pwsh_main", a, "e3")], error_ids={"e3"})
    assert s1 == s3