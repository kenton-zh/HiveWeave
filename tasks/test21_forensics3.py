# TEST21 forensics round 3 (read-only)
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

print('='*70); print('I. AGENTS (fixed cols)'); print('='*70)
for a in q("SELECT short_id, name, role, status, permission_type, substr(parent_id,1,8) par, workspace_path, worktree_error, created_at, last_active_at FROM agents ORDER BY created_at"):
    print(f"[{a['status']:8}] {a['name']:<10}({a['short_id']}) role={a['role'][:22]:<22} {a['permission_type'] or '-':<12} parent={a['par'] or '-':<8} created={ts(a['created_at'])} last_active={ts(a['last_active_at'])}")
    if a['workspace_path']: print(f"    WT: {a['workspace_path']}")
    if a['worktree_error']: print(f"    WT-ERR: {a['worktree_error'][:130]}")

print()
print('='*70); print('J. CEO 归零 17:12 platform feedback (FULL)'); print('='*70)
for r in q("SELECT content FROM chat_messages WHERE agent_id LIKE '8362459b%' AND role='assistant' AND content LIKE '%建议%' AND content LIKE '%CEO%' ORDER BY created_at"):
    print(r['content'])
    print('---')

print()
print('='*70); print('K. 云岫 15:27 feedback (FULL)'); print('='*70)
for r in q("SELECT content FROM chat_messages WHERE agent_id LIKE 'ec607699%' AND role='assistant' AND content LIKE '%可以改进%' ORDER BY created_at LIMIT 2"):
    print(r['content'])
    print('---')

print()
print('='*70); print('L. MERGE PROXY episode'); print('='*70)
for r in q("SELECT substr(agent_id,1,8) aid, role, substr(content,1,300) c, created_at FROM chat_messages WHERE content LIKE '%MERGE PROXY%' ORDER BY created_at LIMIT 5"):
    print(f"  {ts(r['created_at'])} {r['aid']}[{r['role']}]")
    print(f"  {r['c']}")
    print()

print()
print('='*70); print('M. 流川 stuck — full sequence of his last messages'); print('='*70)
for r in q("SELECT role, substr(content,1,250) c, created_at FROM chat_messages WHERE agent_id LIKE 'ac60ceb0%' ORDER BY created_at DESC LIMIT 12"):
    print(f"  {ts(r['created_at'])} [{r['role']}] {r['c'][:240]}")

print()
print('='*70); print('N. 75379d23 backward transition approved->running 15:32:49 context'); print('='*70)
for r in q("SELECT substr(agent_id,1,8) aid, role, substr(content,1,250) c, created_at FROM chat_messages WHERE created_at BETWEEN 1753503100000 AND 1753503600000 AND (content LIKE '%75379d23%' OR content LIKE '%files_changed%' OR content LIKE '%filesChanged%') ORDER BY created_at LIMIT 10"):
    print(f"  {ts(r['created_at'])} {r['aid']}[{r['role']}] {r['c'][:240]}")

print()
print('='*70); print('O. files_changed / filesChanged mentions'); print('='*70)
rows = q("SELECT substr(agent_id,1,8) aid, role, substr(content,1,200) c, created_at FROM chat_messages WHERE content LIKE '%files_changed%' OR content LIKE '%filesChanged%' ORDER BY created_at")
print(f"  total: {len(rows)}")
for r in rows[:20]:
    print(f"  {ts(r['created_at'])} {r['aid']}[{r['role']}] {r['c'][:190]}")

print()
print('='*70); print('P. evidence payload of cancelled tasks (files_changed field)'); print('='*70)
for t in q("SELECT substr(id,1,8) tid, evidence FROM tasks WHERE id IN ('11c57080-fea5-489f-bf2b-edb9d7d222a1','97843f6f-a380-4487-980e-12801501482b','a83d6f9a-2b9f-490b-9065-8bc3d70e3d20','2bf55d12-107c-4c7d-b55d-b393a5257e67')"):
    try:
        ev = json.loads(t['evidence']) if t['evidence'] else {}
        fc = ev.get('files_changed') or ev.get('filesChanged') or []
        print(f"  {t['tid']} files_changed({len(fc)}): {json.dumps(fc, ensure_ascii=False)[:300]}")
        print(f"      keys: {list(ev.keys())}")
    except Exception as e:
        print(f"  {t['tid']} parse err {e}: {(t['evidence'] or '')[:150]}")

print()
print('='*70); print('Q. user_pings / questions schema+data'); print('='*70)
cols = [r[1] for r in conn.execute('PRAGMA table_info(user_pings)')]
print(f'  user_pings cols: {cols}')
for r in q("SELECT * FROM user_pings ORDER BY created_at LIMIT 10"):
    print(' ', dict(r))
cols = [r[1] for r in conn.execute('PRAGMA table_info(agent_runs)')]
print(f'  agent_runs cols: {cols}')
for r in q("SELECT * FROM agent_runs WHERE status='error' LIMIT 5"):
    d = dict(r)
    print(' ', {k: str(v)[:120] for k, v in d.items()})

conn.close()
