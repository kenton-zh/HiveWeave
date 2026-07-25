import sqlite3

c = sqlite3.connect(r"D:\PC_AI\Project\TEST_YLGY\.hiveweave\data.db")
c.row_factory = sqlite3.Row
agents = {
    r["id"]: (r["short_id"], r["name"])
    for r in c.execute("select id,short_id,name from agents")
}
print("OPEN:")
for t in c.execute(
    "select id,status,progress,assignee_id,reviewer_id,"
    "substr(replace(title,char(10),' '),1,60) title "
    "from tasks where status not in ('closed')"
):
    asg = agents.get(t["assignee_id"], ("?", "?"))
    rev = agents.get(t["reviewer_id"] or "", ("-", "-"))
    print(
        t["status"],
        t["progress"],
        "asg=" + asg[0] + asg[1],
        "rev=" + rev[0] + str(rev[1]),
        t["title"],
    )
print(
    "wait_cycle inbox",
    c.execute(
        "select count(*) from inbox where message like '%WAIT_CYCLE%'"
    ).fetchone()[0],
)
print(
    "waive",
    c.execute(
        "select count(*) from run_steps where tool_name='waive_attestation'"
    ).fetchone()[0],
)
print("\nRECENT WORK LOGS:")
for r in c.execute(
    "select substr(agent_id,1,8) a, type, "
    "substr(replace(summary,char(10),' '),1,90) s "
    "from work_logs order by created_at desc limit 8"
):
    print(dict(r))
