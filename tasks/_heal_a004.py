import sqlite3

c = sqlite3.connect(r"D:\PC_AI\Project\TEST12\.hiveweave\data.db")
r = c.execute(
    "SELECT short_id, worktree_error FROM agents WHERE short_id='A004'"
).fetchone()
print("before", r)
c.execute(
    "UPDATE agents SET worktree_error=NULL "
    "WHERE short_id='A004' AND worktree_error IS NOT NULL"
)
c.commit()
r = c.execute(
    "SELECT short_id, worktree_error FROM agents WHERE short_id='A004'"
).fetchone()
print("after", r)
