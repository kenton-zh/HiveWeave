"""Empty done_slice detection + complete wake policy (any message wakes)."""

from hiveweave.services.wake_policy import should_wake


def _is_empty_done_slice_turn(tool_calls: list) -> bool:
    """Mirror Agent._is_empty_done_slice_turn without importing Agent."""
    substantive = {
        "submit_task", "review_task", "claim_task", "create_task",
        "hire_agent", "write_file", "edit_file", "bash", "apply_patch",
        "git_worktree_merge", "ask_agent", "send_message", "approve_work",
        "reject_work", "dispatch_task",
    }
    names: set[str] = set()
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        n = (tc.get("function") or {}).get("name") or tc.get("name") or ""
        if n:
            names.add(n)
    if not names:
        return True
    return not (names & substantive)


def test_empty_done_slice_only_commit_turn():
    assert _is_empty_done_slice_turn(
        [{"function": {"name": "commit_turn"}}]
    )
    assert _is_empty_done_slice_turn([])


def test_empty_done_slice_false_with_substantive():
    assert not _is_empty_done_slice_turn(
        [
            {"function": {"name": "review_task"}},
            {"function": {"name": "commit_turn"}},
        ]
    )


def test_complete_wakes_on_any_message():
    assert should_wake(
        "command", disposition="complete", from_agent_id="peer"
    ) is True
    assert should_wake(
        "task_transition", disposition="complete", from_agent_id="peer"
    ) is True
    assert should_wake(
        "command", disposition="complete", from_agent_id="user"
    ) is True
    assert should_wake(
        "ask", disposition="complete", from_agent_id="peer"
    ) is True
    assert should_wake(
        "progress", disposition="complete", from_agent_id="peer"
    ) is True


def _complete_trigger_allowed(opts: dict) -> bool:
    """Mirror agent.chat complete-skip allow gate via admit_wake."""
    from hiveweave.services.wake_policy import admit_wake

    wake_cat = opts.get("wake_category") or "command"
    if wake_cat not in (
        "command",
        "ask",
        "approval",
        "task_transition",
        "progress",
    ):
        wake_cat = "command"
    admit = admit_wake(
        wake_cat,
        disposition="complete",
        from_agent_id=opts.get("from_agent_id"),
        recipient_parent_id=opts.get("recipient_parent_id"),
    )
    source = opts.get("source") or ""
    is_task_wake = (
        source
        in (
            "task",
            "dispatch",
            "task_transition",
            "inbox_task",
            "verify",
        )
        or opts.get("message_type") == "task"
        or bool(opts.get("task_id"))
    )
    return bool(admit.ok or is_task_wake or opts.get("from_user"))


def test_complete_chat_opts_allow_any_wake_category():
    assert _complete_trigger_allowed({"wake_category": "task_transition"})
    assert _complete_trigger_allowed({"source": "task"})
    assert _complete_trigger_allowed({"message_type": "task"})
    assert _complete_trigger_allowed(
        {"wake_category": "ask", "from_agent_id": "peer"}
    )
    assert _complete_trigger_allowed(
        {"wake_category": "command", "from_agent_id": "peer"}
    )
    assert _complete_trigger_allowed({"source": "trigger"})
    assert _complete_trigger_allowed(
        {"wake_category": "progress", "from_agent_id": "peer"}
    )
