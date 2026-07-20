"""Wait-cycle break: clear ALL SCC members + task-mediated edges (TEST3)."""

from __future__ import annotations

from hiveweave.services.wait_contract import _scc


def test_scc_detects_two_cycle():
    graph = {"a": {"b"}, "b": {"a"}}
    comps = [c for c in _scc(graph) if len(c) >= 2]
    assert len(comps) == 1
    assert set(comps[0]) == {"a", "b"}


def test_scc_detects_three_cycle():
    graph = {"a": {"b"}, "b": {"c"}, "c": {"a"}}
    comps = [c for c in _scc(graph) if len(c) >= 2]
    assert len(comps) == 1
    assert set(comps[0]) == {"a", "b", "c"}


def test_scc_no_cycle():
    graph = {"a": {"b"}, "b": {"c"}, "c": set()}
    comps = [c for c in _scc(graph) if len(c) >= 2]
    assert comps == []
