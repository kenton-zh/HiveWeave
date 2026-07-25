import sqlite3
from collections import Counter

c = sqlite3.connect(r"D:\PC_AI\Project\TEST_YLGY\.hiveweave\data.db")
c.row_factory = sqlite3.Row
cols = [x[1] for x in c.execute("pragma table_info(tool_attestations)")]
print("cols", cols)
for r in c.execute("select kind, count(*) c from tool_attestations group by kind"):
    print(dict(r))
print("total", c.execute("select count(*) from tool_attestations").fetchone()[0])
# sample
for r in c.execute("select kind, substr(coalesce(summary,''),1,60) s from tool_attestations limit 5"):
    try:
        print(dict(r))
    except Exception:
        print([r[k] for k in r.keys()])

# post-window stalls from log - user said 18:27
# exhausted sample via telemetry if any in DB platform?
# agent ids map
for r in c.execute("select id, short_id, name from agents"):
    print(r["short_id"], r["name"], r["id"][:8])
