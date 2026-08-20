"""Pin dispatch messages to agent worktrees."""
from __future__ import annotations

import re

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

    footer = (
        f"\n\n[WORKTREE PIN] Writes: this tree only ({sid}): {rel}\n"
        f"Reads: MAIN docs/ and .hiveweave/shared/ are team-visible."
    )
    if "[WORKTREE PIN]" not in text:
        text = text.rstrip() + footer
    return text
