---
name: h5-game-qa
description: >-
  Test H5/canvas mini-games via deterministic harness scripts plus vision
  asserts. Use when building or QA-ing browser games, Phaser/Pixi/Canvas apps,
  or when realtime AI play is impossible. Enforces __HW_TEST__ /
  render_game_to_text / advanceTime contract and dual code+vision gates.
---

# H5 Game QA

Realtime AI cannot play action games (vision latency ≫ frame time).  
**Scripts drive; vision judges.**

Full contract: [docs/spec/h5-game-test-harness.md](../../../docs/spec/h5-game-test-harness.md)  
Example seam: [contract-example.ts](./contract-example.ts)  
Demo: `apps/hiveweave-py/fixtures/h5_jump_demo/`

## When to use

- Building or iterating on an HTML5 / canvas / Phaser / Pixi game
- QA tasks tagged UI/E2E for game projects
- User asks for drag/swipe/jump testing, level clear verification, etc.

## When NOT to use

- Pure DOM apps (forms, dashboards) → use `browse` / `qa` skills
- Claiming “pass” on action games with no harness hooks

## Authoring games (Executor)

1. Ship cases **in the game repo** (`tests/cases.ts` or equivalent).
2. Expose in test/dev builds only:
   - Preferred: `window.__HW_TEST__` (`list` / `run` / `getState` / optional clock)
   - Compat: `window.render_game_to_text()` + `window.advanceTime(ms)`
3. Prefer simulation `advanceTime` (fixed dt loops), not wall-clock sleeps.
4. Dual asserts per case: `assertCode` + `visionCriteria` string.
5. Action timelines use button bursts (`right`, `space`, mouse coords) — see spec.

## Running QA (HiveWeave / Cursor)

1. Start game server (not ports 4000/5173).
2. `browse goto` game URL (`?hw_test=1`).
3. Prefer **`game_run_case`**:
   - `action="probe"` → tier
   - `action="list"` → case ids
   - `action="run", caseId=...` → codePass + screenshot pixels
4. No hooks → **observe-only**. Do not claim gameplay pass.
5. `assert_visual` with `visionCriteria` from run result.
6. Verdict = `codePass && visionPass`.

### HiveWeave tool sketch

```
browse(args=["goto", "http://127.0.0.1:PORT/?hw_test=1"])
game_run_case(action="probe")
game_run_case(action="list")
game_run_case(action="run", caseId="jump_cross_gap")
assert_visual(
  screenshotPath="evidence/hw-game-jump_cross_gap.png",
  observed="…what pixels show…",
  criteria="Player standing on right platform; not in pit",
  verdict="pass"
)
```

## Tiers

| Tier | Meaning |
|------|---------|
| interactive | Menus / turn-based — normal click/fill OK |
| scripted | Timelines + advanceTime |
| instrumented | Full `__HW_TEST__` |
| observe-only | No hooks — load/crash only |

## Dual gate (hard rule)

| Gate | Source | Failure |
|------|--------|---------|
| Code | `codePass` / state JSON | Fail even if screenshot “looks ok” |
| Vision | `assert_visual` on pixels | Fail even if state JSON ok (render bug) |

Path-only “evidence” is rejected — same as existing `assert_visual` rules.

## Anti-patterns

- Closed-loop AI dodge/aim/rhythm play
- DOM snapshot as sole canvas evidence
- Skipping `advanceTime` and using flaky wall `sleep`
- Soft-passing when harness missing

## Upstream lineage

- OpenAI `develop-web-game` — `render_game_to_text`, `advanceTime`, action bursts
- PlayableIntelligence `game-qa` — fixtures, clock, iterate-client
- Midscene skills — optional vision `aiAssert` layer; not a substitute for harness drive
