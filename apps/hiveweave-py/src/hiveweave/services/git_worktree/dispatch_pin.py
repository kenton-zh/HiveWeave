"""Pin dispatch messages to agent worktrees."""
from __future__ import annotations

import os
import re
from pathlib import Path

# Collapse abs/rel worktree prefixes: D:\proj\.hiveweave\worktrees\A001
# or .hiveweave/worktrees/B12 → relative .hiveweave/worktrees/<assignee>.
# head 只吞「路径字符组成、以分隔符结尾」的前缀（含盘符），防止把紧贴
# 路径的中文/反引号/括号等正文一起吞掉（审计 P1：`请在.xxxx` 丢「请在」）。
# sid 位数不设上限 —— generate_short_id 是 zfill(3) 无上限编号。
_WT_REF = re.compile(
    r"(?P<head>(?:[A-Za-z]:)?[\w\-./\\ ]*?[/\\])?"
    r"\.hiveweave[/\\]+worktrees[/\\]+"
    r"(?P<sid>[A-Za-z]\d{2,})",
    re.IGNORECASE,
)

# TEST_DSH_64 #4：派单 footer 嵌入 MAIN .hiveweave/shared/ 实存契约清单的
# 上限（条数）与扫描上限（防异常大的共享目录拖慢派单）。
_MAIN_SHARED_LIST_CAP = 10
_MAIN_SHARED_SCAN_CAP = 500


def _project_root_from(worktree_path: str) -> str:
    """MAIN 项目根：剥掉 ``.hiveweave/worktrees/<id>`` 前缀；已是根则原样。"""
    p = Path(worktree_path)
    parts = p.parts
    cf = [x.casefold() for x in parts]
    for i in range(len(cf) - 2):
        if cf[i] == ".hiveweave" and cf[i + 1] == "worktrees":
            if i == 0:
                break
            return str(Path(*parts[:i]))
    return str(p)


def _main_shared_contract_files(worktree_path: str) -> list[str]:
    """MAIN 工作区 ``.hiveweave/shared/`` 实存契约文件相对路径（mtime 倒序）。

    派单 footer 嵌入精确路径，assignee 免第一跳撞墙（TEST_DSH_64 #4）。
    任何 IO 失败静默返回 []——footer 不因列举失败而坏。
    """
    try:
        root = _project_root_from(worktree_path)
        shared = Path(root) / ".hiveweave" / "shared"
        if not shared.is_dir():
            return []
        found: list[tuple[float, str]] = []
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(shared):
            dirnames.sort()
            for name in sorted(filenames):
                scanned += 1
                if scanned > _MAIN_SHARED_SCAN_CAP:
                    break
                fp = Path(dirpath) / name
                try:
                    mtime = fp.stat().st_mtime
                    rel = fp.relative_to(shared).as_posix()
                except (OSError, ValueError):
                    continue
                found.append((mtime, rel))
            if scanned > _MAIN_SHARED_SCAN_CAP:
                break
        found.sort(key=lambda item: item[0], reverse=True)
        return [rel for _mt, rel in found[:_MAIN_SHARED_LIST_CAP]]
    except Exception:
        return []


def pin_dispatch_message_to_worktree(
    description: str,
    *,
    short_id: str,
    worktree_path: str,
) -> str:
    """Rewrite wrong worktree paths and append a write-only WORKTREE PIN footer.

    已知限制：负向引用（「不要碰 A001 的树」）中的他人 sid 同样会被改写
    为本 agent sid —— 重写目的是防 dispatcher 指错树，负向语义不区分。
    """
    text = description or ""
    sid = (short_id or "").strip()
    if not sid:
        return text
    rel = f".hiveweave/worktrees/{sid}"

    wp = (worktree_path or "").strip()
    if wp:
        for variant in (wp, wp.replace("\\", "/"), wp.replace("/", "\\")):
            if variant:
                text = text.replace(variant, rel)

    text = _WT_REF.sub(rel, text)
    text = re.sub(
        re.escape(rel) + r"([^\s\"']*)",
        lambda m: rel + m.group(1).replace("\\", "/"),
        text,
    )

    # TEST_DSH_64 #4：读侧口径订正——shared 契约读 `.hiveweave/shared/`
    # （跨树可读，本树没有会自动在 MAIN/兄弟树命中），不再教「MAIN docs/」；
    # 普通 repo 文档合并后自然可见。并嵌入 MAIN shared 实存契约清单。
    reads = (
        "Reads: shared contract files from `.hiveweave/shared/` "
        "(cross-tree readable — if not in your tree it will be found in "
        "MAIN or sibling trees); regular repo docs arrive via git merge."
    )
    shared_files = _main_shared_contract_files(worktree_path)
    if shared_files:
        listing = "\n".join(f"  - .hiveweave/shared/{f}" for f in shared_files)
        reads += (
            "\nShared contracts currently on MAIN (.hiveweave/shared/, "
            f"newest first):\n{listing}"
        )

    footer = (
        f"\n\n[WORKTREE PIN] Writes: this tree only ({sid}): {rel}\n"
        f"Shared: write_file to .hiveweave/shared/<file> in your tree, then "
        f"checkpoint -> merge; once merged it lands in MAIN and becomes "
        f"team-visible. Kept worktrees pick it up via git_worktree_sync / "
        f"recreate — do not promise reads before it is merged.\n"
        f"{reads}"
    )
    if "[WORKTREE PIN]" not in text:
        text = text.rstrip() + footer
    return text
