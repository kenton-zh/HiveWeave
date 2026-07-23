"""Doom loop: same-args fingerprint + narrowed exemption (TEST4, 修 #1)."""

from hiveweave.llm.streamer import Streamer, doom_loop_limit


def test_commit_turn_limit_is_eight():
    assert doom_loop_limit("commit_turn") == 8


def test_detect_doom_same_args_fingerprint():
    tracker: dict = {"last_key": None, "count": 0}
    args = '{"phase":"waiting","summary":"x"}'
    for i in range(7):
        doom = Streamer._detect_doom_loop(
            [{"name": "commit_turn", "arguments": args, "id": f"c{i}"}],
            tracker,
        )
        assert doom is None
    doom = Streamer._detect_doom_loop(
        [{"name": "commit_turn", "arguments": args, "id": "c8"}],
        tracker,
    )
    assert doom == "commit_turn"


def test_detect_doom_resets_on_different_args():
    tracker: dict = {"last_key": None, "count": 0}
    for i in range(5):
        Streamer._detect_doom_loop(
            [
                {
                    "name": "commit_turn",
                    "arguments": f'{{"phase":"waiting","summary":"{i}"}}',
                    "id": f"c{i}",
                }
            ],
            tracker,
        )
    assert tracker["count"] == 1  # each different summary resets


def test_detect_doom_no_exemption_after_error():
    """修 #1: 失败后同参数重试不再豁免，count 照常增长。"""
    tracker: dict = {
        "last_key": ("commit_turn", '{"phase":"done_slice","summary":"a"}'),
        "count": 7,
    }
    doom = Streamer._detect_doom_loop(
        [
            {
                "name": "commit_turn",
                "arguments": '{"phase":"done_slice","summary":"a"}',
                "id": "c1",
            }
        ],
        tracker,
    )
    # 同参数重试不再豁免：count 8 → 触发 doom
    assert doom == "commit_turn"
    assert tracker["count"] == 8
