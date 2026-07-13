"""File management tools: delete, move, mkdir, rmdir, search.

Extracted from executor.py _tool_* methods. Security checks are
auto-injected by the pipeline (security_level="file_op").
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ConfigDict

from .base import tool
from .result import ToolResult


# ── Pydantic models ──────────────────────────────────────


class DeleteFileParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file_path: str = Field(
        alias="filePath",
        description="Path to the file to delete.",
        json_schema_extra={"aliases": ["path", "file_path", "file"]},
    )


class MoveFileParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source_path: str = Field(
        alias="sourcePath",
        description="Source file path.",
        json_schema_extra={"aliases": ["src", "source", "from", "source_path"]},
    )
    destination_path: str = Field(
        alias="destinationPath",
        description="Destination file path.",
        json_schema_extra={"aliases": ["dst", "destination", "to", "destination_path"]},
    )


class CreateDirectoryParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    path: str = Field(
        description="Directory path to create.",
        json_schema_extra={"aliases": ["dirPath", "dir_path", "directory", "dir"]},
    )


class DeleteDirectoryParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    path: str = Field(
        description="Directory path to delete (with contents).",
        json_schema_extra={"aliases": ["dirPath", "dir_path", "directory", "dir"]},
    )


class SearchFilesParams(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    pattern: str = Field(
        description="Glob pattern to match files (e.g. '*.py', '**/*.ts').",
        json_schema_extra={"aliases": ["glob", "match"]},
    )
    directory: str = Field(
        default=".",
        description="Directory to search in (default: workspace root).",
        json_schema_extra={"aliases": ["dir", "path", "dirPath", "dir_path"]},
    )


# ── Tool implementations ────────────────────────────────


@tool(
    "delete_file",
    "Delete a file from the workspace. Cannot delete sensitive files or .hiveweave system files.",
    requires_workspace=True,
    security_level="file_op",
)
async def delete_file_tool(params: DeleteFileParams, agent_id: str, workspace: str) -> ToolResult:
    """Delete a file."""
    from .file import _resolve_safe

    resolved = _resolve_safe(workspace, params.file_path)
    if resolved is None:
        return ToolResult.err(f"Path traversal denied: {params.file_path}")

    target = Path(resolved)
    if not target.exists():
        return ToolResult.err(f"File not found: {params.file_path}")
    if not target.is_file():
        return ToolResult.err(f"Not a file: {params.file_path}")

    try:
        target.unlink()
        return ToolResult.ok(f"Deleted: {params.file_path}")
    except Exception as e:
        return ToolResult.err(f"Failed to delete {params.file_path}: {e}")


@tool(
    "move_file",
    "Move or rename a file. Auto-creates parent directories of destination.",
    requires_workspace=True,
    security_level="file_op",
)
async def move_file_tool(params: MoveFileParams, agent_id: str, workspace: str) -> ToolResult:
    """Move or rename a file."""
    from .file import _resolve_safe

    src_resolved = _resolve_safe(workspace, params.source_path)
    dst_resolved = _resolve_safe(workspace, params.destination_path)
    if src_resolved is None or dst_resolved is None:
        return ToolResult.err("Path traversal denied")

    source = Path(src_resolved)
    dest = Path(dst_resolved)
    if not source.exists():
        return ToolResult.err(f"Source not found: {params.source_path}")

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.rename(dest)
        return ToolResult.ok(f"Moved: {params.source_path} -> {params.destination_path}")
    except Exception as e:
        return ToolResult.err(f"Failed to move: {e}")


@tool(
    "create_directory",
    "Create a new directory (and parent directories if needed).",
    requires_workspace=True,
    security_level="file_op",
)
async def create_directory_tool(params: CreateDirectoryParams, agent_id: str, workspace: str) -> ToolResult:
    """Create a directory."""
    from .file import _resolve_safe

    resolved = _resolve_safe(workspace, params.path)
    if resolved is None:
        return ToolResult.err(f"Path traversal denied: {params.path}")

    try:
        Path(resolved).mkdir(parents=True, exist_ok=True)
        return ToolResult.ok(f"Created directory: {params.path}")
    except Exception as e:
        return ToolResult.err(f"Failed to create directory: {e}")


@tool(
    "delete_directory",
    "Delete a directory and all its contents. Cannot delete .hiveweave system files.",
    requires_workspace=True,
    security_level="file_op",
)
async def delete_directory_tool(params: DeleteDirectoryParams, agent_id: str, workspace: str) -> ToolResult:
    """Delete a directory recursively."""
    from .file import _resolve_safe

    resolved = _resolve_safe(workspace, params.path)
    if resolved is None:
        return ToolResult.err(f"Path traversal denied: {params.path}")

    target = Path(resolved)
    if not target.exists():
        return ToolResult.err(f"Directory not found: {params.path}")
    if not target.is_dir():
        return ToolResult.err(f"Not a directory: {params.path}")

    try:
        shutil.rmtree(target)
        return ToolResult.ok(f"Deleted directory: {params.path}")
    except Exception as e:
        return ToolResult.err(f"Failed to delete directory: {e}")


@tool(
    "search_files",
    "Search for files by glob pattern. Returns matching file paths relative to workspace.",
    requires_workspace=True,
    security_level="file_op",
)
async def search_files_tool(params: SearchFilesParams, agent_id: str, workspace: str) -> ToolResult:
    """Search files by glob pattern."""
    from .file import _resolve_safe, _check_hiveweave_dir, _is_sensitive

    ws = Path(workspace).resolve()
    if params.directory != ".":
        resolved = _resolve_safe(workspace, params.directory)
        if resolved is None:
            return ToolResult.err(f"Path traversal denied: {params.directory}")
        search_dir = Path(resolved)
    else:
        search_dir = ws

    try:
        matches = sorted(search_dir.rglob(params.pattern))
        # Exclude .hiveweave and sensitive files
        matches = [
            m for m in matches[:200]
            if not _check_hiveweave_dir(str(m), workspace)
            and not _is_sensitive(str(m))
        ]
        paths = [str(m.relative_to(ws)) for m in matches[:50]]

        if not paths:
            return ToolResult.ok("No files found matching the pattern.")

        body = f"Found {len(matches)} file(s):\n" + "\n".join(paths)
        return ToolResult.ok(body)
    except Exception as e:
        return ToolResult.err(f"Search failed: {e}")
