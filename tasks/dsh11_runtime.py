"""TEST_DSH_11 agent runtime 快照"""
import json
import urllib.request

BASE = "http://localhost:4000/api/debug"
IDS = {
    "60f2e45b-9350-4ce7-896e-81fe3515cc0d": "A127 澜川",
    "2e7237db-0000-0000-0000-000000000000": "A125 归零(猜)",
}
# 先从 org 拿真实 id
PID = "84b95435-317c-43f3-8a22-68947cfa3981"
with urllib.request.urlopen(f"http://localhost:4000/api/org/{PID}/agents", timeout=30) as r:
    agents = json.loads(r.read().decode("utf-8"))
if isinstance(agents, dict):
    agents = agents.get("agents", [])

for a in agents:
    aid = a["id"]
    name = f"{a.get('short_id')} {a.get('name')}"
    try:
        with urllib.request.urlopen(f"{BASE}/agents/{aid}/runtime", timeout=30) as r:
            rt = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"{name}: ERR {e}")
        continue
    # 精简输出
    keep = {}
    for k in ("execution", "disposition", "status", "busy", "processing",
              "last_active_at", "wake_reason", "streaming", "consecutive_errors",
              "message_queue", "obligations", "waits", "ledger"):
        if k in rt:
            v = rt[k]
            if isinstance(v, (list, dict)):
                s = json.dumps(v, ensure_ascii=False)
                keep[k] = s[:300]
            else:
                keep[k] = v
    print(f"== {name} ==")
    for k, v in keep.items():
        print(f"  {k}: {v}")
    print()
