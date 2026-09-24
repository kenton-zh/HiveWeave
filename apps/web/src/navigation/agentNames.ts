/**
 * 成员显示名缓存 —— 窗口标题 / 固定角标把 agentId 翻译成人名（FE-07）
 *
 * 边界（gamewindow/store §2.1）：窗口层不持有业务状态。但「详情 · 乙（已固定）」
 * 这类标题与角标需要人名，而人名既不在 app store 也不在窗口 store 里 ——
 * org tree 是 OfficeView 自行拉取的。这里维护一份**纯展示用** id→name 缓存：
 * OfficeView 每次 org tree 到手后整体写入；查不到回落 null（调用方降级为
 * 不带名字的标题）。缓存随时可被覆盖，禁止作为任何业务判断依据。
 */

const MAX_ENTRIES = 500;

const names = new Map<string, string>();

/** 批量写入（幂等：同名重复写是 no-op，不触碰 Map 序）。 */
export function rememberAgentNames(entries: Array<{ id: string; name?: string }>): void {
  for (const { id, name } of entries) {
    if (!id || !name) continue;
    if (names.get(id) === name) continue;
    names.set(id, name);
  }
  // 容量护栏：Map 插入序 = FIFO，超限从最老的开始丢
  if (names.size > MAX_ENTRIES) {
    let excess = names.size - MAX_ENTRIES;
    for (const key of names.keys()) {
      if (excess-- <= 0) break;
      names.delete(key);
    }
  }
}

/** 查显示名；未知成员返回 null（调用方降级，不猜名字）。 */
export function agentDisplayName(agentId: string | null | undefined): string | null {
  if (!agentId) return null;
  return names.get(agentId) ?? null;
}
