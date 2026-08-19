"""TEST_DSH_11 project-export 聚合分析（只读）"""
import json
import time
import urllib.request
from collections import Counter

BASE = "http://localhost:4000/api/debug"
PID = "84b95435-317c-43f3-8a22-68947cfa3981"

url = f"{BASE}/project-export?project_id={PID}&truncate=200&max_rows=5000"
with urllib.request.urlopen(url, timeout=60) as r:
    data = json.loads(r.read().decode("utf-8"))

tables = data.get("tables", data)

id2name = {}
for a in tables.get("agents", []):
    id2name[a["id"]] = f"{a.get('short_id')}·{a.get('name')}"

def nm(aid):
    if not aid:
        return "-"
    return id2name.get(aid, str(aid)[:8])

now = time.time() * 1000
def hhmm(ts):
    if not ts:
        return "?"
    return time.strftime("%m-%d %H:%M", time.localtime(ts / 1000))

def age_min(ts):
    if not ts:
        return -1
    return round((now - ts) / 60000)

# ── 任务 ──
print("=== TASKS ===")
for t in tables.get("tasks", []):
    print(f"{t['id'][:8]} | {t.get('title','')[:50]}")
    print(f"  status={t.get('status')} progress={t.get('progress')} assignee={nm(t.get('assignee_id'))} creator={nm(t.get('creator_id'))} created={hhmm(t.get('created_at'))} updated={hhmm(t.get('updated_at'))} (age {age_min(t.get('updated_at'))}min ago)")

# ── task_events ──
evs = tables.get("task_events", [])
print(f"\n=== TASK_EVENTS ({len(evs)}) ===")
for e in evs:
    print(f"{hhmm(e.get('created_at'))} | {nm(e.get('actor_id'))} | {e.get('event_type')} | {e.get('from_status')}->{e.get('to_status')} | {str(e.get('payload') or '')[:80]}")

# ── chat_messages 统计 ──
msgs = tables.get("chat_messages", [])
print(f"\n=== CHAT_MESSAGES ({len(msgs)}) by agent ===")
c = Counter(nm(m.get("agent_id")) for m in msgs)
for k, v in c.most_common():
    print(f"  {k}: {v}")

# ── work_logs ──
wl = tables.get("work_logs", [])
print(f"\n=== WORK_LOGS ({len(wl)}) ===")
for w in wl:
    print(f"{hhmm(w.get('created_at'))} | {nm(w.get('agent_id'))} | [{w.get('type')}] {str(w.get('summary') or '')[:100]}")

# ── inbox 未读/ask ──
ib = tables.get("inbox", [])
unread = [i for i in ib if not i.get("read")]
asks = [i for i in ib if i.get("message_type") == "ask" or i.get("expect_report")]
print(f"\n=== INBOX total={len(ib)} unread={len(unread)} ask/expect_report={len(asks)} ===")
for i in ib[-20:]:
    print(f"{hhmm(i.get('created_at'))} | {nm(i.get('from_agent_id'))}->{nm(i.get('to_agent_id'))} | read={i.get('read')} type={i.get('message_type')} er={i.get('expect_report')} | {str(i.get('message') or '')[:70]}")

# ── agent_runs ──
runs = tables.get("agent_runs", [])
print(f"\n=== AGENT_RUNS ({len(runs)}) ===")
for r_ in runs[-30:]:
    print(f"{hhmm(r_.get('started_at'))} | {nm(r_.get('agent_id'))} | status={r_.get('status')} | dur={r_.get('duration_ms')} | err={str(r_.get('error') or '')[:60]}")

# ── tool attestations 统计 ──
ta = tables.get("tool_attestations", [])
print(f"\n=== TOOL_ATTESTATIONS ({len(ta)}) ===")
tc = Counter((a.get("tool_name"), a.get("status")) for a in ta)
for (tool, st), v in tc.most_common(25):
    print(f"  {tool} [{st}]: {v}")

# ── 最近 30 条对话内容 ──
print("\n=== RECENT CHAT (last 30) ===")
for m in msgs[-30:]:
    print(f"{hhmm(m.get('created_at'))} | {nm(m.get('agent_id'))} [{m.get('role')}] bg={m.get('is_background')} | {str(m.get('content') or '')[:150].replace(chr(10),' ')}")
