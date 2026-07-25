import json
import sqlite3
from pathlib import Path

c = sqlite3.connect(r"D:\PC_AI\Project\TEST12\.hiveweave\data.db")
c.row_factory = sqlite3.Row

print("=== doc_review ===")
for r in c.execute(
    "SELECT id, kind, task_id, agent_id, command_or_url FROM tool_attestations WHERE kind='doc_review'"
):
    print(dict(r))

print("=== waiver-like ===")
for r in c.execute(
    "SELECT id, kind, task_id, agent_id, command_or_url FROM tool_attestations WHERE kind LIKE '%waiv%'"
):
    print(dict(r))

print("=== all kinds counts ===")
for r in c.execute(
    "SELECT kind, COUNT(*) n FROM tool_attestations GROUP BY kind ORDER BY n DESC"
):
    print(dict(r))

print("=== tasks evidence/verify ===")
cols = [x[1] for x in c.execute("PRAGMA table_info(tasks)")]
print([x for x in cols if any(s in x.lower() for s in ("evid", "verif", "attest"))])
for t in c.execute("SELECT id, title, status, evidence FROM tasks"):
    print("---", t["id"][:8], t["status"], (t["title"] or "")[:50])
    ev = t["evidence"]
    if not ev:
        print(" evidence: None")
        continue
    e = json.loads(ev) if isinstance(ev, str) else ev
    if not isinstance(e, dict):
        print(" evidence:", type(e))
        continue
    print(" evidence.keys", sorted(e.keys()))
    for k in (
        "attestation_ids",
        "attestationIds",
        "waiver",
        "waived",
        "waive_reason",
        "tests_passed",
        "files_changed",
        "notes",
        "merged_by",
        "verification_cases",
    ):
        if k in e:
            print(" ", k, str(e[k])[:400])

p = Path(r"D:\PC_AI\Project\TEST12\specs\checkin.md")
print("spec", p.exists(), p.stat().st_size if p.exists() else 0)
print("pngs", [f.name for f in sorted(Path(r"D:\PC_AI\Project\TEST12").glob("*.png"))[:12]])
