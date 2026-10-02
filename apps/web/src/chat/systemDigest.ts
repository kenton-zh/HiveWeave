/**
 * 系统消息 digest 解析（trigger.py 唤醒注入的「## 标题 + JSON lines」格式）。
 *
 * 后端给 LLM 的 digest 每行是一个 JSON 对象（内嵌 \n 转义），前端直出
 * = 一整墙转义文本、无换行。本模块把它解析成结构化条目，供 InboundLetter
 * 渲染成人话卡片；解析失败（非 digest / 任一行坏 JSON）一律返回 null，
 * 调用方回退原文渲染 —— 绝不因美化而丢内容。
 */

export interface DigestEntry {
  from?: string;
  content?: string;
  replyRequired?: boolean;
  reportRequired?: boolean;
  urgent?: boolean;
  /** 8 位短任务 id（后端已截断）。 */
  taskId?: string;
  /** 原始状态值（rejected/submitted/...），渲染侧映射中文。 */
  status?: string;
  /** Open Work / Pending Review 块：我在该任务中的角色（reviewer/creator）。 */
  youAre?: string;
}

export interface DigestSection {
  /** 人类可读标题（已知块映射中文，未知块取 " — " 前的原文）。 */
  title: string;
  entries: DigestEntry[];
  /** 标题下的纯文本行（给 LLM 的操作指引，人读作次要注释）。 */
  notes: string[];
}

export interface SystemDigest {
  sections: DigestSection[];
}

const TITLE_MAP: Array<[RegExp, string]> = [
  [/^Pending Tasks/i, "待办任务"],
  [/^WORK REJECTED/i, "返工要求"],
  [/^Messages/i, "来信"],
  [/^Background updates/i, "同事动态 · 仅供参考"],
  [/^Goals Workbook/i, "目标工作簿更新"],
  [/^Open Work/i, "未收口的开放工作"],
  [/^Pending Review/i, "名下待审任务"],
];

function humanizeTitle(rawHeading: string): string {
  // 标题行 = "## X — 给 LLM 的长说明"；人读只留 " — " 前的短题，
  // 完整原文放 title 属性（hover 可见）。
  const head = rawHeading.replace(/^##\s*/, "").split(" — ")[0].trim();
  for (const [re, zh] of TITLE_MAP) if (re.test(head)) return zh;
  return head;
}

function toEntry(obj: Record<string, unknown>): DigestEntry {
  const str = (v: unknown) => (typeof v === "string" && v ? v : undefined);
  return {
    from: str(obj.from),
    // Open Work / Pending Review 块无 content/task，正文在 title（任务标题）
    content: str(obj.content) ?? str(obj.task) ?? str(obj.title),
    replyRequired: obj.reply_required === true,
    reportRequired: obj.report_required === true,
    urgent: str(obj.priority) === "urgent",
    taskId: str(obj.task_id),
    status: str(obj.status),
    youAre: str(obj.you_are),
  };
}

/**
 * 解析唤醒 digest。判据（全部满足才认）：
 * ① 首个非空行是 `## ` 标题；② 至少一条可 parse 的 JSON 对象行；
 * ③ 所有以 `{` 开头的行都能 parse 成对象（坏 JSON ⇒ 整体回退原文，
 * 避免半渲染的误导）。
 */
export function parseSystemDigest(content: string | undefined | null): SystemDigest | null {
  if (!content) return null;
  const lines = content.split("\n");
  const sections: DigestSection[] = [];
  let current: DigestSection | null = null;
  let entryCount = 0;
  let sawFirstNonEmpty = false;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue;
    if (!sawFirstNonEmpty) {
      if (!line.startsWith("## ")) return null; // 非 digest 开头 ⇒ 快速放弃
      sawFirstNonEmpty = true;
    }
    if (line.startsWith("## ")) {
      current = { title: humanizeTitle(line), entries: [], notes: [] };
      sections.push(current);
      continue;
    }
    if (line.startsWith("{")) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(line);
      } catch {
        return null; // 坏 JSON 行 ⇒ 整体回退
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
      if (!current) {
        current = { title: "系统消息", entries: [], notes: [] };
        sections.push(current);
      }
      current.entries.push(toEntry(parsed as Record<string, unknown>));
      entryCount++;
      continue;
    }
    // 纯文本行（操作指引等）：归入当前块注释；无当前块则回退原文渲染
    if (!current) return null;
    current.notes.push(line);
  }

  return entryCount > 0 ? { sections } : null;
}

/** 折叠态摘要：各块「标题 N 条（来自 X、Y）」拼接。 */
export function digestPreview(digest: SystemDigest): string {
  const parts: string[] = [];
  for (const s of digest.sections) {
    if (s.entries.length > 0) {
      const names = [...new Set(s.entries.map((e) => e.from).filter(Boolean))] as string[];
      parts.push(`${s.title} ${s.entries.length} 条${names.length ? `（${names.join("、")}）` : ""}`);
    } else if (s.notes.length > 0) {
      parts.push(s.title);
    }
  }
  return parts.join(" · ");
}

/** 状态值 → 中文徽章文案（未知值原样显示）。 */
export function digestStatusLabel(status: string): string {
  const map: Record<string, string> = {
    rejected: "已驳回",
    submitted: "已提交",
    approved: "已通过",
    reviewing: "审查中",
    pending: "待处理",
    in_progress: "进行中",
    blocked: "阻塞中",
  };
  return map[status] ?? status;
}

/** 角色值 → 中文文案（Open Work / Pending Review 的 you_are）。 */
export function digestRoleLabel(role: string): string {
  const map: Record<string, string> = {
    reviewer: "我是审查人",
    creator: "我是创建人",
    assignee: "我是执行人",
  };
  return map[role] ?? `我是 ${role}`;
}
