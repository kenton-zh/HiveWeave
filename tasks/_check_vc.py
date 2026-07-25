import sqlite3

c = sqlite3.connect(r"D:\PC_AI\Project\TEST12\.hiveweave\data.db")
c.row_factory = sqlite3.Row
print("count", c.execute("SELECT COUNT(*) FROM verification_cases").fetchone()[0])
for r in c.execute("SELECT * FROM verification_cases"):
    print(dict(r))
