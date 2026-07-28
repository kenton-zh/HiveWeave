# H5 Jump Demo (HiveWeave harness)

Minimal canvas platformer that implements `docs/spec/h5-game-test-harness.md`.

## Cases

| id | Expect |
|----|--------|
| `jump_cross_gap` | Scripted run+jump lands on right platform (`codePass=true`) |
| `fall_in_pit` | Walk into gap without jump → fall (`codePass=true` when dead) |

## Serve

```bash
cd apps/hiveweave-py/fixtures/h5_jump_demo
python -m http.server 3456
```

## Agent / manual tool flow

```text
browse(args=["goto","http://127.0.0.1:3456/?hw_test=1"])
game_run_case(action="probe")
game_run_case(action="list")
game_run_case(action="run", caseId="jump_cross_gap")
assert_visual(screenshotPath="evidence/hw-game-jump_cross_gap.png",
  observed="Blue player on right green platform; pit empty below",
  criteria="Player standing on the right green platform; not in the dark pit; not fallen.",
  verdict="pass")
```

Workspace for screenshots must be a writable project dir (agent worktree). For a quick local check from this folder, set browse cwd / use a temp workspace that contains `evidence/`.
