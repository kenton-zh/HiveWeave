"""B3 ③④：charter 编制结构声明（paradigm/faces/pod_count）——零 schema 变更.

存储形态：dict 可选字段编码成一行「[org-shape] paradigm=… faces=… pod=…」
标记追加进既有 project_rules 列（agent_charters 不加列）；read 侧
parse_org_shape 解析回结构化字段；旧 charter 无标记 → 无这些键 → 跳过校验。
附带 B4：save_charter 重存时 version minor 自增（DELETE+INSERT 不丢版本）。

DB 交互用内存 fake（无真实 sqlite）：INSERT 参数按实现里固定的列序还原
成行 dict，read_charter 的 SELECT 走同一行。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import hiveweave.services.charter as ch
from hiveweave.services.charter import (
    CharterService,
    _next_charter_version,
    _org_shape_marker,
    charter_service,
    parse_org_shape,
)

# 与 save_charter 的 INSERT / read_charter 的 SELECT 列序一致（实现内固定）
_COLS = (
    "id",
    "project_id",
    "agent_id",
    "title",
    "content",
    "status",
    "project_rules",
    "version",
    "created_at",
    "updated_at",
)


class _FakeCursor:
    def __init__(self, row: dict | None) -> None:
        self._row = row

    async def fetchone(self) -> dict | None:
        return self._row

    async def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self, row: dict | None) -> None:
        self._row = row

    async def execute(self, _sql: str, _args: list | None = None):
        return _FakeCursor(self._row)

    async def close(self) -> None:
        pass


@pytest.fixture()
def charter_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Route CharterService at in-memory fakes; return captured state."""
    state: dict = {"inserts": []}

    async def fake_tx(_pid: str, statements: list) -> None:
        for sql, params in statements:
            if "INSERT INTO" in sql:
                state["inserts"].append(list(params))

    async def fake_get_db(_pid: str) -> _FakeConn:
        # 返回最近一次 INSERT 还原出的行（首存前为 None → version 1.0）
        row = (
            dict(zip(_COLS, state["inserts"][-1]))
            if state["inserts"]
            else None
        )
        return _FakeConn(row)

    monkeypatch.setattr(
        ch,
        "agent_router",
        SimpleNamespace(get_project_id=lambda _agent_id: "proj-1"),
    )
    monkeypatch.setattr(
        ch.meta_db, "get_project_workspace", AsyncMock(return_value="/ws")
    )
    monkeypatch.setattr(ch, "execute_transaction_by_project", fake_tx)
    monkeypatch.setattr(ch, "get_project_db_by_project_id", fake_get_db)
    return state


# ── 纯函数：marker 构造 / 解析 / version 自增 ────────────────────


def test_org_shape_marker_build_and_roundtrip():
    m = _org_shape_marker("PM_Architect", 4, None)
    assert m == "[org-shape] paradigm=pm_architect faces=4"
    assert parse_org_shape(m) == {"paradigm": "pm_architect", "faces": 4}

    pod = _org_shape_marker("pod", 3, 2)
    assert parse_org_shape(pod) == {
        "paradigm": "pod",
        "faces": 3,
        "pod_count": 2,
    }
    # 全部字段非法/缺失 → 空串（不产生标记，不拦存）
    assert _org_shape_marker(None, None, None) == ""
    assert _org_shape_marker("", "not-a-number", object()) == ""


def test_parse_org_shape_rules_and_precedence():
    # project_rules 与 content 同时有标记 → content（后参）覆盖
    merged = parse_org_shape(
        "规则一……\n[org-shape] paradigm=solo faces=1",
        "正文……\n[org-shape] paradigm=pod faces=3 pod=2",
    )
    assert merged == {"paradigm": "pod", "faces": 3, "pod_count": 2}
    # 旧 charter：无标记 → {}（调用方跳过校验）
    assert parse_org_shape("普通章程正文", None) == {}
    assert parse_org_shape(None, None) == {}
    # 非法 token 忽略，合法键保留；无效整行忽略
    partial = parse_org_shape(
        "[org-shape] paradigm=tech_lead faces=abc junk",
    )
    assert partial == {"paradigm": "tech_lead"}


def test_next_charter_version_bumps_minor():
    assert _next_charter_version(None) == "1.0"
    assert _next_charter_version("") == "1.0"
    assert _next_charter_version("1.0") == "1.1"
    assert _next_charter_version("1.1") == "1.2"
    assert _next_charter_version("2.9") == "3.0"
    assert _next_charter_version("not-a-version") == "1.0"


# ── save/read 存取往返（fake DB）────────────────────────────────


@pytest.mark.asyncio
async def test_charter_dict_shape_fields_roundtrip(charter_env: dict):
    await charter_service.save_charter(
        "proj-1",
        "agent-1",
        {
            "title": "T",
            "content": "正文",
            "paradigm": "tech_lead",
            "faces": 4,
        },
    )
    # 零 schema 变更：标记行落进 project_rules 列
    assert charter_env["inserts"], "INSERT not captured"
    row = dict(zip(_COLS, charter_env["inserts"][-1]))
    assert "[org-shape] paradigm=tech_lead faces=4" in row["project_rules"]
    assert row["version"] == "1.0"

    got = await CharterService().read_charter("proj-1")
    assert got is not None
    assert got["paradigm"] == "tech_lead"
    assert got["faces"] == 4
    assert "pod_count" not in got  # 未声明 → 无键
    assert "正文" in got["content"]


@pytest.mark.asyncio
async def test_charter_resave_bumps_version_and_updates_shape(charter_env: dict):
    svc = CharterService()
    await svc.save_charter(
        "proj-1",
        "agent-1",
        {"title": "T", "content": "v1", "paradigm": "solo", "faces": 1},
    )
    await svc.save_charter(
        "proj-1",
        "agent-1",
        {"title": "T", "content": "v2", "paradigm": "pod", "faces": 3, "pod_count": 2},
    )
    assert charter_env["inserts"][-1][_COLS.index("version")] == "1.1"

    got = await svc.read_charter("proj-1")
    assert got is not None
    assert got["version"] == "1.1"
    assert got["paradigm"] == "pod"
    assert got["pod_count"] == 2


@pytest.mark.asyncio
async def test_charter_plain_str_save_has_no_shape_keys(charter_env: dict):
    await charter_service.save_charter("proj-1", "agent-1", "纯文本章程")
    got = await CharterService().read_charter("proj-1")
    assert got is not None
    assert "paradigm" not in got
    assert "faces" not in got
    assert got["content"] == "纯文本章程"


@pytest.mark.asyncio
async def test_charter_content_inline_marker_parsed_on_read(charter_env: dict):
    """agent 可达声明通道：正文里写一行 [org-shape] 标记即可被解析。"""
    await charter_service.save_charter(
        "proj-1",
        "agent-1",
        "项目章程……\n[org-shape] paradigm=pm_architect faces=5",
    )
    got = await CharterService().read_charter("proj-1")
    assert got is not None
    assert got["paradigm"] == "pm_architect"
    assert got["faces"] == 5
