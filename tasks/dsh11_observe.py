"""TEST_DSH_11 团队表现观察脚本（只读诊断）"""
import json
import time
import urllib.request

BASE = "http://localhost:4000/api"
PID = "84b95435-317c-43f3-8a22-68947cfa3981"


def get(url: str):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


now_ms = int(time.time() * 1000)

# 1. agents
agents = get(f"{BASE}/org/{PID}/agents")
if isinstance(agents, dict):
    agents = agents.get("agents", agents.get("data", []))
print("=== AGENTS ===")
for a in agents:
    print(
        f"{a.get('short_id')} | {a.get('name')} | role={a.get('role')} | "
        f"perm={a.get('permission_type')} | status={a.get('status')} | "
        f"parent={str(a.get('parent_id'))[:8]}"
    )

# 2. tasks
tasks = get(f"{BASE}/projects/{PID}/tasks")
if isinstance(tasks, dict):
    tasks = tasks.get("tasks", tasks.get("data", []))
print(f"\n=== TASKS ({len(tasks)}) ===")
for t in tasks:
    print(
        f"{str(t.get('id'))[:8]} | {t.get('title','')[:40]} | status={t.get('status')} | "
        f"progress={t.get('progress')} | assignee={str(t.get('assignee_id'))[:8]} | "
        f"creator={str(t.get('creator_id'))[:8]} | kind={t.get('task_type') or t.get('kind')}"
    )

# 3. timeline activity
act = get(
    f"{BASE}/projects/{PID}/timeline/activity?since_ms=0&until_ms={now_ms}&limit=2000"
)
events = act.get("events", act if isinstance(act, list) else [])
print(f"\n=== ACTIVITY EVENTS ({len(events)}) truncated={act.get('truncated') if isinstance(act, dict) else '?'} ===")
# 聚合：按 agent 和事件类型
from collections import Counter

by_agent = Counter()
by_type = Counter()
for e in events:
    by_agent[e.get("agent_name") or str(e.get("agent_id"))[:8]] += 1
    by_type[e.get("type") or e.get("event_type")] += 1
print("-- by agent --")
for k, v in by_agent.most_common():
    print(f"  {k}: {v}")
print("-- by type --")
for k, v in by_type.most_common():
    print(f"  {k}: {v}")

# 4. 最近 40 条事件明细
print("\n=== LAST 40 EVENTS ===")
for e in events[-40:]:
    ts = e.get("ts") or e.get("at") or 0
    tstr = time.strftime("%H:%M:%S", time.localtime(ts / 1000)) if ts else "?"
    print(
        f"{tstr} | {e.get('agent_name') or str(e.get('agent_id'))[:8]} | "
        f"{e.get('type') or e.get('event_type')} | {str(e.get('summary') or e.get('detail') or '')[:100]}"
    )
