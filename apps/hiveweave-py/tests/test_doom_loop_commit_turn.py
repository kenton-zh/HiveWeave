"""Doom loop: same-args fingerprint + gate-retry exemption (TEST4)."""

from hiveweave.llm.streamer import Streamer, doom_loop_limit


def test_commit_turn_limit_is_eight():
    assert doom_loop_limit("commit_turn") == 8


def test_detect_doom_same_args_fingerprint():
    tracker: dict = {"last_key": None, "count": 0, "last_errored": False}
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
    tracker: dict = {"last_key": None, "count": 0, "last_errored": False}
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


def test_detect_doom_exempts_after_error():
    tracker: dict = {
        "last_key": ("commit_turn", '{"phase":"done_slice","summary":"a"}'),
        "count": 7,
        "last_errored": True,
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
    # Failed retry does not increment — still below limit after exemption
    assert doom is None
    assert tracker["count"] == 7
    assert tracker["last_errored"] is False
