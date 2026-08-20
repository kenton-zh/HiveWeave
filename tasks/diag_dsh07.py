import sqlite3
from datetime import datetime, timezone

PDB = r"D:\PC_AI\Project\HiveTestProject\TEST_DSH_07\.hiveweave\data.db"
conn = sqlite3.connect(PDB)
conn.row_factory = sqlite3.Row

def ts(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d %H:%M:%S")

print("=== AGENTS ===")
for a in conn.execute("SELECT short_id, name, role, status, permission_type, last_active_at, created_at, workspace_path, worktree_error FROM agents ORDER BY short_id"):
    print(f"[{a['short_id']}] {a['name']} role={a['role']} status={a['status']} perm={a['permission_type']}")
    print(f"    created={ts(a['created_at'])} last_active={ts(a['last_active_at'])}")

print("\n=== TASKS ===")
try:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    for t in conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall():
        d = dict(t)
        print(f"[{d.get('task_id')}] {d.get('title')} status={d.get('status')} assignee={d.get('assignee_id')}")
        print(f"    created={ts(d.get('created_at'))} updated={ts(d.get('updated_at'))} depends={d.get('depends_on')}")
except Exception as e:
    print("ERR", e)

print("\n=== AGENT WAITS ===")
try:
    for w in conn.execute("SELECT * FROM agent_waits ORDER BY created_at").fetchall():
        d = dict(w)
        print(f"agent={d.get('agent_id')} state={d.get('state')} ref={d.get('ref')} expires={ts(d.get('expires_at'))} created={ts(d.get('created_at'))}")
except Exception as e:
    print("ERR", e)

print("\n=== CHAT MESSAGES (last 20) ===")
for m in conn.execute("SELECT role, is_background, is_streaming, substr(content,1,160) as c, created_at FROM chat_messages ORDER BY created_at DESC LIMIT 20"):
    print(f"[{ts(m['created_at'])}] role={m['role']} bg={m['is_background']} streaming={m['is_streaming']}: {m['c']}")

print("\n=== INBOX (last 15) ===")
for i in conn.execute("SELECT from_agent_id, to_agent_id, read, message_type, substr(message,1,140) as m, created_at FROM inbox ORDER BY created_at DESC LIMIT 15"):
    print(f"[{ts(i['created_at'])}] from={i['from_agent_id']} to={i['to_agent_id']} read={i['read']} type={i['message_type']} reply_req={i['reply_required']}: {i['m']}")

print("\n=== WORK LOGS (last 15) ===")
for l in conn.execute("SELECT agent_id, type, substr(summary,1,140) as s, created_at FROM work_logs ORDER BY created_at DESC LIMIT 15"):
    print(f"[{ts(l['created_at'])}] {l['agent_id']} type={l['type']}: {l['s']}")

print("\n=== ALARMS ===")
try:
    for al in conn.execute("SELECT * FROM alarms ORDER BY created_at").fetchall():
        d = dict(al)
        print(f"agent={d.get('agent_id')} due={ts(d.get('wake_at'))} created={ts(d.get('created_at'))} status={d.get('status')} reason={d.get('reason')}")
except Exception as e:
    print("ERR", e)

conn.close()
