/**
 * Stream idle timeout watchdog — verification test.
 *
 * Run: pnpm -C packages/agent-runtime exec tsx test/with-idle-timeout.test.ts
 *
 * Verifies:
 *   T1: Normal stream passes through unchanged.
 *   T2: Stream that never yields (silent hang) → StreamIdleTimeoutError (first-chunk phase).
 *   T3: Stream that yields one chunk then hangs → StreamIdleTimeoutError (idle phase).
 *   T4: Iterator release on timeout — underlying iterator's return() is called.
 *   T5: Shorter idle threshold (after first chunk) than first-chunk threshold.
 *   T6: Normal early break (consumer stops consuming) also releases iterator.
 */

import { withIdleTimeout, StreamIdleTimeoutError, isStreamIdleTimeout } from "../src/stream-timeout.js";

// ── Helpers ────────────────────────────────────────────────────────────────

/** Build an async iterable from an array of {value, delay} steps. */
async function* timedStream(steps: Array<{ value: string; delay: number } | { hang: true }>): AsyncGenerator<string> {
  for (const step of steps) {
    if ("hang" in step) {
      // Simulate silent hang with a long sleep. The idle watchdog's timeout
      // will fire first; this sleep just keeps Node's event loop alive so the
      // process doesn't exit prematurely. 5s is well beyond any test's
      // firstChunk/idle threshold (max 2000ms).
      //
      // NOTE: we deliberately do NOT clean up this timer in a finally — the
      // async-generator spec (tc39/proposal-async-iteration#126) says .return()
      // cannot interrupt an `await` of a pending promise, so the generator
      // stays suspended until the sleep settles, then quietly completes.
      await sleep(5_000);
      return;
    }
    if (step.delay > 0) await sleep(step.delay);
    yield step.value;
  }
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

let passed = 0;
let failed = 0;

function assert(cond: boolean, msg: string) {
  if (cond) {
    passed++;
    console.log(`  PASS ${msg}`);
  } else {
    failed++;
    console.log(`  FAIL ${msg}`);
  }
}

async function collectOrThrow<T>(iter: AsyncGenerator<T>): Promise<{ ok: true; values: T[] } | { ok: false; err: unknown }> {
  const values: T[] = [];
  try {
    for await (const v of iter) values.push(v);
    return { ok: true, values };
  } catch (err) {
    return { ok: false, err };
  }
}

// ── Tests ─────────────────────────────────────────────────────────────────

async function test1_normalStream() {
  console.log("[T1] Normal stream passes through unchanged");
  const src = timedStream([
    { value: "a", delay: 10 },
    { value: "b", delay: 10 },
    { value: "c", delay: 10 },
  ]);
  const wrapped = withIdleTimeout(src, 1000, 1000);
  const result = await collectOrThrow(wrapped);
  assert(result.ok, "stream completed without timeout");
  assert(result.ok && result.values.join("") === "abc", "all values preserved: abc");
}

async function test2_neverYieldsHangs() {
  console.log("[T2] Stream that never yields -> first-chunk timeout");
  const src = timedStream([{ hang: true }]);
  const wrapped = withIdleTimeout(src, 200, 200); // 200ms first-chunk timeout
  const t0 = Date.now();
  const result = await collectOrThrow(wrapped);
  const elapsed = Date.now() - t0;
  assert(!result.ok, "threw an error");
  assert(!result.ok && result.err instanceof StreamIdleTimeoutError, "threw StreamIdleTimeoutError");
  assert(!result.ok && isStreamIdleTimeout(result.err), "isStreamIdleTimeout() recognizes it");
  assert(!result.ok && (result.err as StreamIdleTimeoutError).isfirstChunk === true, "isFirstChunk=true (first-chunk phase)");
  assert(elapsed >= 180 && elapsed < 500, `fired around 200ms (got ${elapsed}ms)`);
}

async function test3_yieldThenHang() {
  console.log("[T3] Stream yields one chunk then hangs -> idle timeout (not first-chunk)");
  const src = timedStream([
    { value: "first", delay: 10 },
    { hang: true }, // yields "first", then hangs (5s sleep)
  ]);
  const wrapped = withIdleTimeout(src, 2000, 200); // 2s first-chunk, 200ms idle
  const t0 = Date.now();
  const result = await collectOrThrow(wrapped);
  const elapsed = Date.now() - t0;
  assert(!result.ok, "threw an error");
  assert(!result.ok && result.err instanceof StreamIdleTimeoutError, "threw StreamIdleTimeoutError");
  assert(!result.ok && (result.err as StreamIdleTimeoutError).isfirstChunk === false, "isFirstChunk=false (idle phase)");
  assert(elapsed >= 180 && elapsed < 2000, `fired around 200ms after first chunk (got ${elapsed}ms)`);
}

async function test4_iteratorReleasedOnTimeout() {
  console.log("[T4] Underlying iterator.return() called on timeout (no fd leak)");
  let returnCalled = false;
  const src: AsyncGenerator<string> = {
    // Long-ish sleep (longer than the 100ms timeout) keeps Node alive; the
    // iterator's return() resolves immediately, so fire-and-forget still
    // observes returnCalled=true.
    async next() { await sleep(5_000); return { done: true, value: undefined as any }; },
    async return() { returnCalled = true; return { done: true, value: undefined as any }; },
    throw(e?: any) { return Promise.reject(e); },
    [Symbol.asyncIterator]() { return this; },
  };
  const wrapped = withIdleTimeout(src, 100, 100);
  await collectOrThrow(wrapped);
  assert(returnCalled, "underlying iterator.return() was called");
}

async function test5_earlyBreakReleasesIterator() {
  console.log("[T5] Consumer break also releases underlying iterator");
  let returnCalled = false;
  const src: AsyncGenerator<string> = {
    async next() { await sleep(10); return { done: false, value: "x" }; },
    async return() { returnCalled = true; return { done: true, value: undefined as any }; },
    throw(e?: any) { return Promise.reject(e); },
    [Symbol.asyncIterator]() { return this; },
  } as any;

  const wrapped = withIdleTimeout(src, 10000, 10000);
  let count = 0;
  for await (const _ of wrapped) {
    count++;
    if (count >= 3) break; // consumer breaks early
  }
  assert(count === 3, `consumed 3 items before break (got ${count})`);
  assert(returnCalled, "iterator.return() called after early break");
}

async function test6_firstChunkThresholdDistinctFromIdle() {
  console.log("[T6] First-chunk threshold can be much longer than idle threshold");
  // Long first-chunk wait (300ms) is allowed by firstChunkMs=500 but would be killed by idleMs=100.
  // This proves the two-threshold design: long first-token waits tolerated, but post-first-chunk stalls are not.
  const src = timedStream([
    { value: "first-after-300ms", delay: 300 },
    { hang: true }, // yields first, then hangs (5s sleep) — idle timeout fires
  ]);
  const wrapped = withIdleTimeout(src, 500, 100); // 500ms first-chunk, 100ms idle
  const t0 = Date.now();
  const result = await collectOrThrow(wrapped);
  const elapsed = Date.now() - t0;
  assert(!result.ok, "threw (idle timeout after first chunk)");
  assert(elapsed >= 380 && elapsed < 1000, `first chunk arrived (300ms) then idle fired ~100ms later (got ${elapsed}ms)`);
  assert(!result.ok && (result.err as StreamIdleTimeoutError).isfirstChunk === false, "isFirstChunk=false (idle phase, not first-chunk)");
}

// ── Runner ─────────────────────────────────────────────────────────────────

async function main() {
  const fs = await import("fs");

  // Intercept ALL console.log/console.error output — stdout capture on Windows
  // is unreliable (truncated scrollback, partial redirect). Accumulating here
  // and writing to an absolute path file at the end is the only reliable way
  // to verify results in CI / headless runs.
  const lines: string[] = [];
  const origLog = console.log.bind(console);
  const origErr = console.error.bind(console);
  console.log = (...args: any[]) => { const s = args.join(" "); lines.push(s); origLog(s); };
  console.error = (...args: any[]) => { const s = args.join(" "); lines.push(s); origErr(s); };

  console.log("=== withIdleTimeout verification ===");
  await test1_normalStream();
  await test2_neverYieldsHangs();
  await test3_yieldThenHang();
  await test4_iteratorReleasedOnTimeout();
  await test5_earlyBreakReleasesIterator();
  await test6_firstChunkThresholdDistinctFromIdle();

  console.log(`\n=== Results: ${passed} passed, ${failed} failed ===`);

  // Absolute path at project root — no cwd ambiguity, no dir-existence dependency.
  const outPath = "d:/PC_AI/Project/HiveWeave/test_results_abs.txt";
  fs.writeFileSync(outPath, lines.join("\n") + "\n", "utf-8");
  console.log(`(full results written to ${outPath})`);

  if (failed > 0) process.exit(1);
}

main().catch((err) => {
  console.error("Fatal:", err);
  process.exit(1);
});
