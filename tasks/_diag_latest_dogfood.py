"""Inspect latest dogfood project after P2 prompt."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx

API = "http://127.0.0.1:4000"


def main() -> None:
    r = httpx.get(f"{API}/api/projects", timeout=15)
    r.raise_for_status()
    data = r.json()
    projects = data.get("projects", data) if isinstance(data, dict) else data
    print("=== PROJECTS ===")
    for p in projects:
        print(
            (p.get("id") or "")[:8],
            repr(p.get("name")),
            "started=",
            p.get("is_started"),
            "ws=",
            (p.get("workspace_path") or "")[:70],
        )

    # Prefer started, else newest by name TEST*
    active = [p for p in projects if p.get("is_started")]
    target = active[0] if active else (projects[0] if projects else None)
    if not target:
        print("no projects")
        return

    # Prefer highest TEST number if multiple started
    def _key(p):
        name = p.get("name") or ""
        return name

    if len(active) > 1:
        target = sorted(active, key=_key, reverse=True)[0]

    pid = target["id"]
    ws = Path(target["workspace_path"])
    pdb = ws / ".hiveweave" / "data.db"
    print("\n=== TARGET ===", target.get("name"), pid, ws)
    if not pdb.exists():
        print("no db", pdb)
        return

    c = sqlite3.connect(str(pdb))
    c.row_factory = sqlite3.Row

    print("\n=== AGENTS ===")
    for a in c.execute(
        "SELECT short_id, name, role, status, permission_type, "
        "workspace_path, worktree_error FROM agents "
        "WHERE COALESCE(status,'')!='archived' ORDER BY short_id"
    ):
        wp = a["workspace_path"]
        ok = bool(wp and Path(wp).exists() and (Path(wp) / ".git").exists())
        print(
            a["short_id"],
            a["name"],
            a["role"],
            a["permission_type"],
            "wt_ok=",
            ok,
            "err=",
            (a["worktree_error"] or "")[:80] or None,
        )

    print("\n=== TASKS ===")
    for t in c.execute(
        "SELECT id, status, title, tags, progress, is_archived FROM tasks "
        "ORDER BY created_at"
    ):
        print(
            (t["id"] or "")[:8],
            t["status"],
            "prog=",
            t["progress"],
            "arch=",
            t["is_archived"],
            "tags=",
            t["tags"],
            "|",
            (t["title"] or "")[:60],
        )

    print("\n=== VERIFICATION_CASES ===")
    try:
        for r0 in c.execute("SELECT * FROM verification_cases ORDER BY created_at"):
            d = dict(r0)
            print(
                "status=",
                d.get("status"),
                "orig=",
                (d.get("original_task_id") or "")[:8],
                "verify=",
                (d.get("verify_task_id") or "")[:8],
                "merge=",
                (d.get("merge_commit_hash") or "")[:12],
                "notes=",
                (d.get("review_notes") or "")[:100],
            )
    except Exception as e:
        print("vc err", e)

    print("\n=== ATTEST KINDS ===")
    try:
        for r0 in c.execute(
            "SELECT kind, COUNT(*) n FROM tool_attestations GROUP BY kind"
        ):
            print(dict(r0))
        for r0 in c.execute(
            "SELECT kind, task_id, substr(command_or_url,1,120) c "
            "FROM tool_attestations WHERE kind IN ('doc_review','waiver') "
            "ORDER BY created_at"
        ):
            print(dict(r0))
    except Exception as e:
        print("att err", e)

    ceo = c.execute(
        "SELECT id, name FROM agents WHERE role='ceo' AND "
        "COALESCE(status,'')!='archived' LIMIT 1"
    ).fetchone()
    if not ceo:
        print("no ceo")
        return
    ceo_id = ceo["id"]
    print("\n=== CEO", ceo["name"], ceo_id[:8], "===")

    try:
        rt = httpx.get(f"{API}/api/debug/agents/{ceo_id}/runtime", timeout=10)
        print("RUNTIME", json.dumps(rt.json(), ensure_ascii=False)[:800])
    except Exception as e:
        print("runtime", e)

    print("\n=== CEO VISIBLE MSGS ===")
    for m in c.execute(
        "SELECT role, is_background, content, created_at FROM chat_messages "
        "WHERE agent_id=? ORDER BY created_at DESC LIMIT 20",
        (ceo_id,),
    ):
        content = m["content"] or ""
        if m["role"] == "assistant" and content.strip() in (
            "(turn committed)",
            "好的，开始处理。",
        ):
            print("---", m["created_at"], "assistant placeholder:", content.strip())
            continue
        if m["role"] == "user" and m["is_background"] and (
            content.startswith("## Messages") or content.startswith("## Goals")
        ):
            continue
        print(
            "---",
            m["created_at"],
            m["role"],
            "bg=",
            m["is_background"],
            "len=",
            len(content),
        )
        print(content[:2000])
        print()

    print("\n=== ALL RUNTIMES ===")
    for a in c.execute(
        "SELECT id, short_id, name FROM agents WHERE COALESCE(status,'')!='archived'"
    ):
        rr = httpx.get(f"{API}/api/debug/agents/{a['id']}/runtime", timeout=8)
        if rr.status_code != 200:
            print(a["short_id"], rr.status_code)
            continue
        j = rr.json()
        print(
            a["short_id"],
            a["name"],
            "exec=",
            j.get("execution"),
            "disp=",
            j.get("disposition"),
            "waits=",
            len(j.get("waits") or []),
            "obl=",
            len(j.get("obligations") or []),
        )
    c.close()


if __name__ == "__main__":
    main()
