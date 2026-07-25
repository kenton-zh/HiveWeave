import sqlite3
from pathlib import Path
c=sqlite3.connect(r"D:\PC_AI\Project\TEST_YLGY\.hiveweave\data.db")
c.row_factory=sqlite3.Row
print("open tasks:")
for r in c.execute("SELECT substr(id,1,8) id, status, progress, substr(replace(title,char(10),' '),1,50) t FROM tasks WHERE status NOT IN ('closed')"):
    print(dict(r))
print("streaming", c.execute("SELECT count(*) FROM chat_messages WHERE is_streaming=1").fetchone()[0])
print("archived agents", c.execute("SELECT count(*) FROM agents WHERE status='archived'").fetchone()[0])
