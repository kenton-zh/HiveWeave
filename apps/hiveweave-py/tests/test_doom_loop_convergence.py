"""R3 P0-1 收敛判据回归：fail_signature_for_round 语义。"""
from __future__ import annotations

from hiveweave.llm.streamer.doom_loop import fail_signature_for_round


def _call(name: str, args: object, cid: str) -> dict:
    return {"name": name, "id": cid, "arguments": args}


def test_signature_ignores_successful_calls():
    calls = [
        _call("read_file", {"filePath": "a.md"}, "ok-tc"),
        _call("bash", {"command": "pytest x.py"}, "err-tc"),
    ]
    sig = fail_signature_for_round(calls, error_ids={"err-tc"})
    assert sig == ("bash", '{"command": "pytest x.py"}'[:60])


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