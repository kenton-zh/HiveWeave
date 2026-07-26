# TEST21 forensics round 2 (read-only)
import sqlite3, json
from datetime import datetime

DB = 'file:///D:/PC_AI/Project/TEST21/.hiveweave/data.db?mode=ro'
conn = sqlite3.connect(DB, uri=True)
conn.row_factory = sqlite3.Row

def q(sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except Exception as e:
        print(f'  [SQL ERR] {e}'); return []

def ts(v):
    if v is None: return '?'
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v/1000 if v > 1e12 else v).strftime('%H:%M:%S')
        return str(v)[5:19]
    except Exception:
        return str(v)[:19]

print('='*70); print('A. AGENTS ROSTER'); print('='*70)
agent_names = {}
for a in q("SELECT id, short_id, name, role, status, permission_type, permission_mode, substr(parent_id,1,8) par, workspace_path, worktree_error, created_at, last_active_at, archived_at FROM agents ORDER BY created_at"):
    agent_names[a['id'][:8]] = f"{a['name']}({a['short_id']})"
    arch = ts(a['archived_at']) if a['archived_at'] else '-'
    print(f"[{a['status']:8}] {a['name']:<10}({a['short_id']}) role={a['role'][:24]:<24} {a['permission_type'] or '-':<12}/{a['permission_mode'] or '-':<8} parent={a['par'] or '-':<8} created={ts(a['created_at'])} last={ts(a['last_active_at'])} arch={arch}")
    if a['worktree_error']:
        print(f"    WT-ERR: {a['worktree_error'][:120]}")
    if a['workspace_path']:
        print(f"    WT: {a['workspace_path']}")

print()
print('='*70); print('B. TASK EVENTS full history'); print('='*70)
for e in q("SELECT substr(task_id,1,8) tid, event_type, from_status, to_status, substr(actor_id,1,8) act, substr(payload,1,100) p, created_at FROM task_events ORDER BY created_at"):
    print(f"  {ts(e['created_at'])} {e['tid']} {e['event_type']:<14} {e['from_status'] or '-'}->{e['to_status'] or '-'} by={e['act'] or '-':8} {e['p']}")

print()
print('='*70); print('C. AGENT EVENTS (all types summary + detail of non-routine)'); print('='*70)
for r in q("SELECT event_type, COUNT(*) c FROM agent_events GROUP BY event_type ORDER BY c DESC"):
    print(f"  {r['event_type']}: {r['c']}")
print('--- detail ---')
for e in q("SELECT substr(agent_id,1,8) aid, event_type, substr(payload,1,150) p, created_at FROM agent_events ORDER BY created_at DESC LIMIT 60"):
    print(f"  {ts(e['created_at'])} {e['aid']} {e['event_type']:<28} {e['p']}")

print()
print('='*70); print('D. OBLIGATIONS'); print('='*70)
for r in q("SELECT substr(owner_agent_id,1,8) aid, obligation_type, substr(task_id,1,8) tid, status, escalation_count, created_at, fulfilled_at FROM obligations ORDER BY created_at"):
    print(f"  {ts(r['created_at'])} {r['aid']} [{r['status']:<10}] {r['obligation_type']} task={r['tid']} esc={r['escalation_count']} fulfilled={ts(r['fulfilled_at']) if r['fulfilled_at'] else '-'}")

print()
print('='*70); print('E. CANCELLED TASKS — why'); print('='*70)
for t in q("SELECT substr(id,1,8) tid, substr(title,1,40) t, status, blocked_reason, archived_reason, retry_count, substr(evidence,1,200) ev, updated_at FROM tasks WHERE status='cancelled' ORDER BY updated_at"):
    print(f"  {ts(t['updated_at'])} {t['tid']} retry={t['retry_count']} blocked_reason={t['blocked_reason']} archived_reason={t['archived_reason']}")
    print(f"      title: {t['t']}")
    if t['ev']: print(f"      evidence: {t['ev'][:180]}")

print()
print('='*70); print('F. STALL / STUCK / TIMEOUT episodes in chat'); print('='*70)
for kw in ['STALL BREAK', 'AGENT STUCK', '连续超时', 'safety_timeout', 'stream_timeout', 'watchdog', '误报']:
    print(f'--- {kw} ---')
    for r in q("SELECT substr(agent_id,1,8) aid, role, substr(content,1,200) c, created_at FROM chat_messages WHERE content LIKE ? ORDER BY created_at LIMIT 8", (f'%{kw}%',)):
        print(f"  {ts(r['created_at'])} {r['aid']}[{r['role']}] {r['c'][:190]}")

print()
print('='*70); print('G. ERROR runs detail (ac60ceb0 = ?)'); print('='*70)
for r in q("SELECT substr(agent_id,1,8) aid, status, substr(error,1,200) e, created_at FROM agent_runs WHERE status='error' ORDER BY created_at"):
    print(f"  {ts(r['created_at'])} {r['aid']} {r['e']}")

print()
print('='*70); print('H. USER PINGS / QUESTIONS'); print('='*70)
for r in q("SELECT substr(agent_id,1,8) aid, substr(message,1,120) m, created_at FROM user_pings ORDER BY created_at LIMIT 15"):
    print(f"  {ts(r['created_at'])} {r['aid']}: {r['m']}")
for r in q("SELECT substr(agent_id,1,8) aid, substr(question,1,120) qu, substr(answer,1,80) an, status, created_at FROM questions ORDER BY created_at LIMIT 15"):
    print(f"  {ts(r['created_at'])} {r['aid']} [{r['status']}] Q: {r['qu']} A: {r['an']}")

conn.close()
