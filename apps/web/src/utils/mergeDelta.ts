export function mergeDeltaContent(prev: string, next: string): string {
  if (next === prev) return prev;
  if (prev && next.startsWith(prev)) return next;
  if (next && prev.startsWith(next)) return prev;
  if (prev && next && prev.length < 4096) {
    const merged = prev + next;
    if (merged.length > prev.length * 4 && merged.length > 1024) return next;
    return merged;
  }
  return prev + next;
}

export function mergeContentChunks(chunks: string[]): string {
  if (chunks.length <= 1) return chunks[0] || "";

  const out: string[] = [];
  for (const chunk of chunks) {
    const last = out[out.length - 1];
    if (last && chunk.startsWith(last) && chunk.length >= last.length) {
      out[out.length - 1] = chunk;
    } else if (last && last.startsWith(chunk) && last.length >= chunk.length) {
      // already covered
    } else {
      out.push(chunk);
    }
  }
  return out.join("");
}
