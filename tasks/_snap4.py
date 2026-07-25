import sqlite3
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

c = sqlite3.connect(r"D:\PC_AI\Project\TEST_YLGY\.hiveweave\data.db")
c.row_factory = sqlite3.Row
agents = {
    r["id"]: (r["short_id"], r["name"])
    for r in c.execute("select id,short_id,name,status,worktree_error from agents")
}
print("AGENTS:")
for r in c.execute(
    "select short_id,name,status,worktree_error from agents order by short_id"
):
    print(r["short_id"], r["name"], r["status"], "err=", (r["worktree_error"] or "")[:80])

print("\nSTATUS COUNTS:")
for r in c.execute("select status, count(*) c from tasks group by status"):
    print(dict(r))

print("\nOPEN:")
for t in c.execute(
    "select id,status,progress,assignee_id,reviewer_id,"
    "substr(replace(title,char(10),' '),1,70) title "
    "from tasks where status not in ('closed') order by created_at"
):
    asg = agents.get(t["assignee_id"], ("?", "?"))
    rev = agents.get(t["reviewer_id"] or "", ("-", "-"))
    print(t["status"], t["progress"], "asg="+asg[0], "rev="+rev[0], t["title"])

print("\ncounts: dismiss_steps", c.execute("select count(*) from run_steps where tool_name='dismiss_agent'").fetchone()[0])
print("waive", c.execute("select count(*) from run_steps where tool_name='waive_attestation'").fetchone()[0])
print("hire", c.execute("select count(*) from run_steps where tool_name='hire_agent'").fetchone()[0])
print("get_platform_state", c.execute("select count(*) from run_steps where tool_name='get_platform_state'").fetchone()[0])
print("contract_json", c.execute("select count(*) from tasks where contract_json is not null and trim(contract_json)!=''").fetchone()[0])
print("streaming", c.execute("select count(*) from chat_messages where is_streaming=1").fetchone()[0])
print("archived", c.execute("select count(*) from agents where status='archived'").fetchone()[0])
print("uncleared waits", c.execute("select count(*) from agent_waits where cleared_at is null").fetchone()[0])

print("\nRECENT INBOX:")
for i in c.execute(
    "select datetime(created_at/1000,'unixepoch','localtime') ts, "
    "substr(from_agent_id,1,8) frm, substr(to_agent_id,1,8) too, "
    "substr(replace(coalesce(message,''), char(10), ' '),1,100) m "
    "from inbox order by created_at desc limit 12"
):
    print(i["ts"], i["frm"], "->", i["too"], i["m"])

print("\nRECENT WORK LOGS:")
for r in c.execute(
    "select datetime(created_at/1000,'unixepoch','localtime') ts, "
    "substr(agent_id,1,8) a, type, "
    "substr(replace(summary,char(10),' '),1,100) s "
    "from work_logs order by created_at desc limit 10"
):
    print(r["ts"], r["a"], r["type"], r["s"])
