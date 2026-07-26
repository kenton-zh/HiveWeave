# TEST21 per-project DB forensics (read-only)
import sqlite3, json
from datetime import datetime

DB = 'file:///D:/PC_AI/Project/TEST21/.hiveweave/data.db?mode=ro'
conn = sqlite3.connect(DB, uri=True)
conn.row_factory = sqlite3.Row

def q(sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except Exception as e:
        print(f'  [SQL ERR] {e}')
        return []

def ts(v):
    if v is None: return '?'
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v/1000 if v > 1e12 else v).strftime('%m-%d %H:%M:%S')
        return str(v)[:19]
    except Exception:
        return str(v)[:19]

print('='*70)
print('1. AGENTS ROSTER')
print('='*70)
for a in q("SELECT short_id, name, role, status, permission_type, parent_id, workspace_path IS NOT NULL as has_wt, worktree_error, hired_at, archived_at FROM agents ORDER BY hired_at"):
    print(f"[{a['status']:8}] {a['short_id'] or '?':7} {a['name'][:20]:20} role={a['role'][:28]:28} perm={a['permission_type'] or '-':10} wt={a['has_wt']} wterr={(a['worktree_error'] or '-')[:40]} hired={ts(a['hired_at'])} arch={ts(a['archived_at']) if a['archived_at'] else '-'}")

print()
print('='*70)
print('2. TASKS LEDGER')
print('='*70)
rows = q("SELECT id, substr(title,1,44) t, status, progress, substr(assignee_id,1,8) asg, substr(creator_id,1,8) cr, created_at, updated_at FROM tasks ORDER BY created_at")
for r in rows:
    print(f"#{r['id']:<4} [{r['status']:10}] p={r['progress'] or 0:<3} asg={r['asg'] or '-':8} cr={r['cr'] or '-':8} {ts(r['created_at'])}->{ts(r['updated_at'])}  {r['t']}")
print()
for r in q("SELECT status, COUNT(*) c FROM tasks GROUP BY status"):
    print(f"  {r['status']}: {r['c']}")

print()
print('='*70)
print('3. TASK EVENTS (transitions, last 60)')
print('='*70)
for e in q("SELECT task_id, event, substr(detail,1,110) d, created_at FROM task_events ORDER BY created_at DESC LIMIT 60"):
    print(f"  {ts(e['created_at'])} task={e['task_id']:<4} {e['event']:<20} {e['d']}")

print()
print('='*70)
print('4. CHAT MESSAGES stats per agent')
print('='*70)
for r in q("SELECT substr(agent_id,1,8) aid, role, COUNT(*) c, SUM(is_background) bg, SUM(CASE WHEN is_streaming=1 THEN 1 ELSE 0 END) streaming FROM chat_messages GROUP BY agent_id, role ORDER BY c DESC"):
    print(f"  agent={r['aid']} role={r['role']:10} msgs={r['c']:<5} bg={r['bg'] or 0:<4} streaming_left={r['streaming']}")

print()
print('='*70)
print('5. ORPHAN STREAMING messages (is_streaming=1)')
print('='*70)
for r in q("SELECT substr(agent_id,1,8) aid, substr(content,1,80) c, created_at FROM chat_messages WHERE is_streaming=1"):
    print(f"  {ts(r['created_at'])} agent={r['aid']} {r['c']}")

print()
print('='*70)
print('6. AGENT EVENTS (errors/warnings, last 50)')
print('='*70)
for e in q("SELECT substr(agent_id,1,8) aid, type, substr(payload,1,130) p, created_at FROM agent_events ORDER BY created_at DESC LIMIT 50"):
    print(f"  {ts(e['created_at'])} {e['aid']} {e['type']:<26} {e['p']}")

print()
print('='*70)
print('7. WORK LOGS (last 40)')
print('='*70)
for l in q("SELECT substr(agent_id,1,8) aid, type, substr(summary,1,110) s, created_at FROM work_logs ORDER BY created_at DESC LIMIT 40"):
    print(f"  {ts(l['created_at'])} {l['aid']} [{l['type']}] {l['s']}")

print()
print('='*70)
print('8. INBOX unreplied / expect_report')
print('='*70)
for r in q("SELECT substr(from_agent_id,1,8) f, substr(to_agent_id,1,8) t, read, expect_report, message_type, substr(message,1,80) m, created_at FROM inbox WHERE expect_report=1 OR message_type='ask' ORDER BY created_at DESC LIMIT 25"):
    print(f"  {ts(r['created_at'])} {r['f']}->{r['t']} read={r['read']} type={r['message_type']}: {r['m']}")
cnt = q("SELECT COUNT(*) c FROM inbox")[0]['c']
unread = q("SELECT COUNT(*) c FROM inbox WHERE read=0")[0]['c']
print(f"  total inbox={cnt}, unread={unread}")

print()
print('='*70)
print('9. AGENT WAITS / OBLIGATIONS')
print('='*70)
for r in q("SELECT substr(agent_id,1,8) aid, wake_on, expires_at, obligation_version, created_at FROM agent_waits ORDER BY created_at DESC LIMIT 15"):
    print(f"  wait {r['aid']} wake_on={r['wake_on']} exp={ts(r['expires_at'])} ov={r['obligation_version']} {ts(r['created_at'])}")
for r in q("SELECT substr(agent_id,1,8) aid, kind, substr(ref,1,30) ref, status, created_at FROM obligations ORDER BY created_at DESC LIMIT 20"):
    print(f"  oblig {r['aid']} [{r['status']}] {r['kind']} ref={r['ref']} {ts(r['created_at'])}")

print()
print('='*70)
print('10. CONVERSATION TURNS / RUNS')
print('='*70)
for r in q("SELECT substr(agent_id,1,8) aid, COUNT(*) c FROM conversation_turns GROUP BY agent_id ORDER BY c DESC LIMIT 10"):
    print(f"  turns agent={r['aid']}: {r['c']}")
for r in q("SELECT substr(agent_id,1,8) aid, status, COUNT(*) c FROM agent_runs GROUP BY agent_id, status ORDER BY c DESC LIMIT 15"):
    print(f"  runs agent={r['aid']} [{r['status']}]: {r['c']}")

print()
print('='*70)
print('11. STALL / HEALTH signals in chat content')
print('='*70)
for kw in ['STALL BREAK', 'AGENT STUCK', 'MERGE PROXY', 'TASK ADVANCE', 'UNREPLIED', 'offDuty', 'stream_total_timeout', 'circuit']:
    rows = q("SELECT COUNT(*) c FROM chat_messages WHERE content LIKE ?", (f'%{kw}%',))
    print(f"  '{kw}': {rows[0]['c'] if rows else '?'}")

print()
print('='*70)
print('12. RUN TIMELINE (first/last activity)')
print('='*70)
r = q("SELECT MIN(created_at) lo, MAX(created_at) hi FROM chat_messages")[0]
print(f"  chat_messages: {ts(r['lo'])} -> {ts(r['hi'])}")
r = q("SELECT MIN(created_at) lo, MAX(created_at) hi FROM task_events")[0]
print(f"  task_events:   {ts(r['lo'])} -> {ts(r['hi'])}")

conn.close()
