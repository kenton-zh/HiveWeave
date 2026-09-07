"""悬浮球 API + 球页面静态托管（spec §10 P0，打包税 #2）。

静态托管：球页面（HTML/JS/CSS）由后端 :4000 直接服务（打包版与 dev
同一来源）；目录解析顺序 = ``HIVEWEAVE_BALL_STATIC_DIR`` env → 仓库
``apps/desktop/ball/``（dev）→ EXE 同级 ``ball/``（打包）→ 内置兜底页。
dev 的主界面照常走 :5173 Vite，不受影响。

REST（/api/ball/*，ApiKeyAuth 覆盖范围内）：
- GET  /api/ball/state          对话目标（助理 + 各项目 CEO）+ 未读
- POST /api/ball/chat           入站投递（deliver_user_message，source=ball）
- GET  /api/ball/unread         红点计数快照
- POST /api/ball/unread/clear   展开=已读，清计数
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import structlog

log = structlog.get_logger(__name__)

router = APIRouter(tags=["ball"])

_MIME_BY_SUFFIX = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
}

_FALLBACK_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>HiveWeave Ball</title></head>
<body style="font-family:system-ui;padding:1rem">
<h3>HiveWeave 悬浮球</h3>
<p>球页面静态资源未找到（apps/desktop/ball/ 缺失）。
可设置 <code>HIVEWEAVE_BALL_STATIC_DIR</code> 指向资源目录。</p>
<p><a href="/api/health">后端健康检查</a></p>
</body></html>"""


def resolve_ball_static_dir() -> Path | None:
    """球页面静态资源目录解析（打包税 #2：URL 配置化、来源统一 :4000）。"""
    explicit = (os.environ.get("HIVEWEAVE_BALL_STATIC_DIR") or "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(expand_windows_env(explicit)))
    else:
        # dev：仓库 apps/desktop/ball/（config.py parents[4] = 仓库根）
        try:
            from hiveweave.config import _repo_root

            candidates.append(_repo_root() / "apps" / "desktop" / "ball")
        except Exception:
            pass
        # 打包：EXE 同级 ball/（PyInstaller datas 或安装布局）
        if getattr(sys, "frozen", False):
            candidates.append(
                Path(sys.executable).resolve().parent / "ball"
            )
    for c in candidates:
        try:
            if (c / "index.html").is_file():
                return c
        except OSError:
            continue
    return None


def expand_windows_env(path: str) -> str:
    """%VAR% 展开（与 api/filesystem 同款最小实现，避免跨模块耦合）。"""
    import re as _re

    def _replacer(m: "_re.Match[str]") -> str:
        return os.environ.get(m.group(1), m.group(0))

    return _re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", _replacer, path)


def _serve_file(rel: str) -> Response:
    root = resolve_ball_static_dir()
    if root is None:
        return HTMLResponse(_FALLBACK_HTML, status_code=200)
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not target.is_file():
        if rel.endswith("/") or rel == "":
            return HTMLResponse(_FALLBACK_HTML, status_code=200)
        raise HTTPException(status_code=404, detail="Not found")
    suffix = target.suffix.lower()
    media_type = _MIME_BY_SUFFIX.get(suffix, "application/octet-stream")
    return FileResponse(target, media_type=media_type)


@router.get("/ball", include_in_schema=False, response_model=None)
async def ball_index() -> Response:
    """球页面入口（悬浮球壳默认加载 URL）。"""
    return _serve_file("index.html")


@router.get("/ball/{rel_path:path}", include_in_schema=False, response_model=None)
async def ball_asset(rel_path: str) -> Response:
    """球页面静态资源（js/css/...）。"""
    return _serve_file(rel_path)


# ── REST ─────────────────────────────────────────────────────


class BallChatBody(BaseModel):
    content: str
    agentId: str | None = None
    source: str = "ball"


class BallClearBody(BaseModel):
    agentId: str | None = None


async def _ensure_bridge() -> None:
    from hiveweave.services.ball_bridge import start_ball_bridge

    await start_ball_bridge()


@router.get("/api/ball/state")
async def ball_state() -> dict:
    """对话目标（助理 + 各项目 CEO）+ 未读计数（展开态标签数据源）。"""
    from hiveweave.services.assistant import list_ball_targets
    from hiveweave.services.ball_bridge import get_unread_counts

    await _ensure_bridge()
    targets = await list_ball_targets()
    counts = get_unread_counts()
    for p in targets["projects"]:
        if p["ceo"]:
            p["ceo"]["unread"] = counts.get(p["ceo"]["agentId"], 0)
    targets["assistant"]["unread"] = counts.get(
        targets["assistant"]["agentId"], 0
    )
    return targets


@router.post("/api/ball/chat")
async def ball_chat(body: BallChatBody) -> dict:
    """悬浮球入站消息（spec §4.1：入站走 deliver_user_message(source=ball)）。

    agentId 为空或=助理 → 助理对话入口；否则投递到指定 agent
    （展开态 CEO 标签）。agent 未启动时先补启动（总线回调）。
    """
    from hiveweave.services.assistant import (
        ASSISTANT_AGENT_ID,
        chat_with_assistant,
        ensure_agent_started,
    )
    from hiveweave.services.user_message import deliver_user_message

    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")

    await _ensure_bridge()
    target_id = (body.agentId or "").strip() or ASSISTANT_AGENT_ID
    if target_id == ASSISTANT_AGENT_ID:
        return await chat_with_assistant(
            content, source=body.source or "ball"
        )

    # CEO / 其他 agent 标签：先确保启动（assistant.ensure_agent_started
    # 挂总线回调，红点桥与网页端共享事件）
    if not await ensure_agent_started(target_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    result = await deliver_user_message(
        target_id, content, body.source or "ball"
    )
    result["agentId"] = target_id
    return result


@router.get("/api/ball/unread")
async def ball_unread(
    agentId: str | None = Query(default=None),
) -> dict:
    """红点计数快照（球态轮询）。agentId 过滤单 agent。"""
    from hiveweave.services.ball_bridge import get_unread_counts

    counts = get_unread_counts()
    if agentId:
        counts = {agentId: counts.get(agentId, 0)}
    return {"counts": counts, "total": sum(counts.values())}


@router.post("/api/ball/unread/clear")
async def ball_unread_clear(body: BallClearBody) -> dict:
    """清零未读（展开面板 = 已读）。agentId 为空清全部。"""
    from hiveweave.services.ball_bridge import clear_unread

    cleared = clear_unread(body.agentId)
    return {"ok": True, "cleared": cleared}
