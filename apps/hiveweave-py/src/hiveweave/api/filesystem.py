"""Filesystem operation endpoints (contract 19, group 16).

契约 19: Filesystem — 浏览 + 读 + 写 + grep（路径限定在项目工作空间内）
- GET  /api/filesystem/browse?path=&projectId=   列目录
- GET  /api/filesystem/read?path=&projectId=     读文件
- POST /api/filesystem/write                     写文件
- GET  /api/filesystem/grep?pattern=&path=&projectId=  内容搜索
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import structlog

from hiveweave.db import meta as meta_db

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/filesystem", tags=["filesystem"])

#: 单次读取最大字节数（防 OOM）
_MAX_READ_BYTES = 512 * 1024
#: 单次 grep 最大返回行数
_MAX_GREP_LINES = 200


def _resolve_safe(workspace: str, rel_path: str) -> Path:
    """把 rel_path 解析到 workspace 内的绝对路径，拒绝越界。"""
    ws = Path(workspace).resolve()
    target = (ws / rel_path).resolve()
    try:
        target.relative_to(ws)
    except ValueError:
        raise HTTPException(
            status_code=403, detail="Path escapes project workspace"
        )
    return target


async def _workspace_for(project_id: str) -> str:
    workspace = await meta_db.get_project_workspace(project_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Project workspace not found")
    return workspace


@router.get("/browse")
async def browse(
    path: str = Query(default=""),
    projectId: str = Query(...),
) -> dict:
    """列目录（返回 entries: name/type/size）。"""
    workspace = await _workspace_for(projectId)
    target = _resolve_safe(workspace, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if target.is_file():
        return {"path": path, "entries": [], "isFile": True}
    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            # 跳过 .hiveweave 内部目录
            if child.name == ".hiveweave":
                continue
            try:
                stat = child.stat()
                size = stat.st_size
            except OSError:
                size = 0
            entries.append(
                {
                    "name": child.name,
                    "type": "directory" if child.is_dir() else "file",
                    "size": size,
                }
            )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    return {"path": path, "entries": entries, "isFile": False}


@router.get("/read")
async def read_file(
    path: str = Query(...),
    projectId: str = Query(...),
) -> dict:
    """读文件（限 512KB，二进制返回 base64? 此处按文本返回）。"""
    workspace = await _workspace_for(projectId)
    target = _resolve_safe(workspace, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        data = target.read_bytes()[:_MAX_READ_BYTES]
        text = data.decode("utf-8", errors="replace")
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    return {
        "path": path,
        "content": text,
        "size": target.stat().st_size,
        "truncated": target.stat().st_size > _MAX_READ_BYTES,
    }


class WriteFileBody(BaseModel):
    projectId: str
    path: str
    content: str
    append: bool = False


@router.post("/write")
async def write_file(body: WriteFileBody) -> dict:
    """写文件（append=True 追加，否则覆盖）。"""
    workspace = await _workspace_for(body.projectId)
    target = _resolve_safe(workspace, body.path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if body.append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(body.content)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Write failed: {e}")
    return {"ok": True, "path": body.path, "bytes": len(body.content)}


@router.get("/grep")
async def grep_files(
    pattern: str = Query(...),
    path: str = Query(default=""),
    projectId: str = Query(...),
    caseInsensitive: bool = Query(default=False),
) -> dict:
    """在工作空间内递归搜索文本（限 200 行匹配）。"""
    workspace = await _workspace_for(projectId)
    root = _resolve_safe(workspace, path)
    if not root.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    flags = re.IGNORECASE if caseInsensitive else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        raise HTTPException(status_code=422, detail=f"Invalid regex: {e}")

    matches: list[dict] = []
    search_root = root if root.is_dir() else root.parent
    try:
        for candidate in search_root.rglob("*"):
            if not candidate.is_file():
                continue
            # 跳过 .hiveweave / 二进制大头文件
            if ".hiveweave" in candidate.parts:
                continue
            try:
                if candidate.stat().st_size > _MAX_READ_BYTES:
                    continue
                text = candidate.read_text(encoding="utf-8", errors="ignore")
            except (OSError, PermissionError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    try:
                        rel = str(candidate.relative_to(workspace)).replace("\\", "/")
                    except ValueError:
                        rel = str(candidate)
                    matches.append({"file": rel, "line": lineno, "text": line[:500]})
                    if len(matches) >= _MAX_GREP_LINES:
                        return {
                            "matches": matches,
                            "truncated": True,
                            "count": len(matches),
                        }
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    return {"matches": matches, "truncated": False, "count": len(matches)}
