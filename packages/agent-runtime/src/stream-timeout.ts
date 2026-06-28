/**
 * Stream idle timeout utilities (防线 ②).
 *
 * Wraps an async iterable with an idle watchdog: if no item is yielded within
 * the active threshold, throws `StreamIdleTimeoutError`. This catches the
 * "silent hang" failure mode where a provider's stream stays open but never
 * produces chunks (half-open TCP connection, provider-side stall) — the root
 * cause of the original zombie-PROCESSING bug.
 *
 * Two thresholds (DO NOT collapse into one — see test6 for why):
 *   - `firstChunkMs`: applies BEFORE the first chunk arrives. Set high to
 *     tolerate long thinking-model first-token latencies (o1, Claude thinking
 *     can take 60-90s before emitting anything). Setting this as low as
 *     `idleMs` will false-positive on every thinking-model turn.
 *   - `idleMs`: applies BETWEEN consecutive chunks after the first. Should be
 *     much lower — once streaming has started, gaps >60s almost always indicate
 *     a stall, not legitimate reasoning.
 *
 * Iterator release on timeout is fire-and-forget (see comment on the finally
 * block below for why `await it.return()` is incorrect and re-introduces the
 * hang). Real connection cleanup relies on Defense Line ① (provider fetch
 * AbortSignal).
 *
 * After timeout, the error propagates to the caller's catch block, where it
 * has no HTTP status → falls into `classifyNetworkError` → retryable → the
 * existing retry chain (backoff + retry event) handles recovery automatically.
 */

/** Error thrown when the stream idle timeout fires. */
export class StreamIdleTimeoutError extends Error {
  readonly idleMs: number;
  readonly isfirstChunk: boolean;

  constructor(idleMs: number, isFirstChunk: boolean) {
    const phase = isFirstChunk ? "first-chunk" : "idle";
    super(`STREAM_IDLE_TIMEOUT_${phase}_${idleMs}ms`);
    this.name = "StreamIdleTimeoutError";
    this.idleMs = idleMs;
    this.isfirstChunk = isFirstChunk;
  }
}

/**
 * Wrap an async iterable with an idle timeout.
 *
 * @param stream        The source async iterable (e.g. `result.fullStream`).
 * @param firstChunkMs  Max wait for the first item. Tolerates long-thinking first tokens.
 * @param idleMs        Max wait between consecutive items after the first.
 * @yields Items from the source stream.
 * @throws {StreamIdleTimeoutError} when no item arrives within the timeout window.
 */
export async function* withIdleTimeout<T>(
  stream: AsyncIterable<T>,
  firstChunkMs: number,
  idleMs: number,
): AsyncGenerator<T> {
  const it = stream[Symbol.asyncIterator]();
  let firstReceived = false;
  try {
    while (true) {
      const timeoutMs = firstReceived ? idleMs : firstChunkMs;
      let timer: ReturnType<typeof setTimeout> | undefined;
      const timeout = new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new StreamIdleTimeoutError(timeoutMs, !firstReceived)),
          timeoutMs,
        );
      });
      try {
        const result = await Promise.race([it.next(), timeout]);
        if (result.done) return;
        firstReceived = true;
        yield result.value;
      } finally {
        if (timer) clearTimeout(timer);
      }
    }
  } finally {
    // Release the underlying iterator whether we exit normally, on timeout,
    // or via an outer break/return. This prevents fd/connection leaks.
    //
    // IMPORTANT: fire-and-forget — do NOT `await` the return() promise.
    // Per the async-generator spec (tc39/proposal-async-iteration#126), when a
    // generator is suspended at an `await` of a never-settling promise (the
    // exact "silent hang" case this watchdog exists to catch), `.return()` does
    // NOT interrupt the await — it returns a promise that never resolves.
    // Awaiting it would re-introduce the very hang we're trying to escape.
    //
    // For well-behaved iterators (AI SDK streams, normal generators) return()
    // resolves immediately and cleanup is synchronous-ish. For hung iterators,
    // actual resource cleanup relies on Defense Line ① (the provider fetch's
    // AbortSignal) — that is the only reliable way to tear down a stuck HTTP
    // connection.
    try {
      const ret = it.return?.();
      if (ret instanceof Promise) {
        // Swallow rejections from abandoned iterators; we're already tearing down.
        ret.catch(() => {});
      }
    } catch {
      // Iterator had no return() or it threw synchronously — nothing to do.
    }
  }
}

/**
 * Check whether an error is a stream idle timeout.
 * Used by retry classification to decide retryability.
 */
export function isStreamIdleTimeout(err: unknown): err is StreamIdleTimeoutError {
  return err instanceof StreamIdleTimeoutError;
}
