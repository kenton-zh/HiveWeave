import { describe, it, expect } from "vitest";
import {
  appendToolCallSegment,
  applyToolResult,
  beginStreamRound,
  collectFileAttachments,
  collectMessageImages,
  draftFromStreamingMessage,
  extractToolFilePath,
  FILE_CHIP_SUMMARY_MAX,
  isTeamChannelMessage,
  mapDbToChatMessages,
  mergeStreamDraftIntoMessages,
  nextBadgePopToken,
  sanitizeMessagesForCache,
  settledMessageHasSegments,
  shouldWriteChatCache,
  streamEventBackgroundFlag,
  streamEventIsBackground,
  toolResultFirstLine,
  tryParseToolCalls,
} from "./messageUtils";
import type { ChatMessage, StreamDraft } from "./types";

describe("beginStreamRound", () => {
  it("round>=1 时插入 round_boundary 段（不再是伪 text），且不丢弃任何已有内容", () => {
    const draft: StreamDraft = {
      assistantId: "a1",
      segments: [
        { type: "thinking", content: "plan v1" },
        { type: "text", content: "用户选了方案2" },
        { type: "tool_call", tool: { tool: "get_tasks", input: {} } },
      ],
    };
    const next = beginStreamRound(draft, 1);
    expect(next.assistantId).toBe("a1");
    // 全量保留（DSH 整轮视图）+ 尾部轮次边界段
    expect(next.segments.slice(0, 3)).toEqual(draft.segments);
    expect(next.segments[3]).toEqual({ type: "round_boundary", round: 1 });
    // 原 draft 不被突变
    expect(draft.segments).toHaveLength(3);
  });

  it("round 0（首轮）与缺号 no-op——首轮无需分隔", () => {
    const draft: StreamDraft = {
      assistantId: "a1",
      segments: [{ type: "text", content: "首轮旁白" }],
    };
    expect(beginStreamRound(draft, 0)).toBe(draft);
    expect(beginStreamRound(draft, undefined)).toBe(draft);
  });

  it("同 round 二次调用不重复插（TEST_DSH_44 Bug#2 防御带：replay 乱序重放）", () => {
    // replay 重放的 round_start 乱序到达：轮线已在段序列中部也必须识别
    let draft: StreamDraft = {
      assistantId: "a1",
      segments: [{ type: "text", content: "第一轮旁白" }],
    };
    draft = beginStreamRound(draft, 1);
    draft = {
      ...draft,
      segments: [...draft.segments, { type: "text", content: "第二轮旁白" }],
    } as StreamDraft;
    // 二次同轮 round_start（replay 重放）：段序列已含 round 1 边界 → no-op
    const twice = beginStreamRound(draft, 1);
    expect(twice).toBe(draft);
    expect(twice.segments.filter((s) => s.type === "round_boundary")).toHaveLength(1);
    // 尾部边界已存在的同款场景同样去重
    const tailDraft: StreamDraft = {
      assistantId: "a1",
      segments: [{ type: "text", content: "x" }, { type: "round_boundary", round: 2 }],
    };
    expect(beginStreamRound(tailDraft, 2)).toBe(tailDraft);
  });

  it("不同 round 正常插入（去重只拦同轮号）", () => {
    let draft: StreamDraft = {
      assistantId: "a1",
      segments: [
        { type: "text", content: "第一轮" },
        { type: "round_boundary", round: 1 },
        { type: "text", content: "第二轮" },
      ],
    };
    draft = beginStreamRound(draft, 2);
    expect(draft.segments[draft.segments.length - 1]).toEqual({ type: "round_boundary", round: 2 });
    expect(draft.segments.filter((s) => s.type === "round_boundary")).toHaveLength(2);
  });

  it("round_boundary 是独立段——后续 text_delta 不并入，也不会再产生伪 text 标记", () => {
    let draft: StreamDraft = {
      assistantId: "a1",
      segments: [{ type: "text", content: "第一轮旁白" }],
    };
    draft = beginStreamRound(draft, 1);
    // 模拟 useChatMessages 的 text_delta 合并逻辑：last.type === "text"
    // 才并段；round_boundary 段使 delta 另起新 text 段
    const last = draft.segments[draft.segments.length - 1];
    const segType = "text" as const;
    const willMergeIntoLast = !!last && last.type === segType;
    expect(willMergeIntoLast).toBe(false);
    const withDelta: StreamDraft = {
      ...draft,
      segments: [...draft.segments, { type: segType, content: "第二轮旁白开始" }],
    };
    // 轮内旁白独立成段，边界段不携带任何文本
    expect(withDelta.segments[1]).toEqual({ type: "round_boundary", round: 1 });
    expect(withDelta.segments[2]).toEqual({ type: "text", content: "第二轮旁白开始" });
    expect(
      withDelta.segments.some((s) => s.type === "text" && (s.content || "").includes("——")),
    ).toBe(false);
  });
});

describe("appendToolCallSegment", () => {
  const empty: StreamDraft = { assistantId: "a1", segments: [] };

  it("dedups by toolCallId on replay", () => {
    const once = appendToolCallSegment(empty, { tool: "get_tasks", input: {} }, "tc-1");
    const twice = appendToolCallSegment(once, { tool: "get_tasks", input: { x: 1 } }, "tc-1");
    expect(twice.segments).toHaveLength(1);
    expect(twice.segments[0].tool).toEqual({ tool: "get_tasks", input: {}, id: "tc-1" });
    expect(twice).toBe(once);
  });

  it("appends twice when id is missing (repeat calls of the same tool)", () => {
    const once = appendToolCallSegment(empty, { tool: "bash", input: { command: "ls" } });
    const twice = appendToolCallSegment(once, { tool: "bash", input: { command: "ls" } });
    expect(twice.segments).toHaveLength(2);
    expect(twice.segments.every((s) => s.tool?.tool === "bash" && !s.tool?.id)).toBe(true);
  });

  it("keeps distinct ids as separate chips", () => {
    const once = appendToolCallSegment(empty, { tool: "read_file", input: { path: "a" } }, "tc-a");
    const twice = appendToolCallSegment(once, { tool: "read_file", input: { path: "b" } }, "tc-b");
    expect(twice.segments).toHaveLength(2);
    expect(twice.segments.map((s) => s.tool?.id)).toEqual(["tc-a", "tc-b"]);
  });
});

describe("applyToolResult", () => {
  const draft: StreamDraft = {
    assistantId: "a1",
    segments: [
      { type: "tool_call", tool: { tool: "bash", input: {}, id: "c1", status: "running" } },
      { type: "tool_call", tool: { tool: "bash", input: {}, id: "c2", status: "running" } },
      { type: "tool_call", tool: { tool: "read_file", input: {}, status: "running" } },
    ],
  };

  it("updates the segment matched by tool_call_id (id path, no name needed)", () => {
    const next = applyToolResult(draft, "c1", undefined, false, "boom");
    expect(next.segments[0].tool?.status).toBe("error");
    expect(next.segments[0].tool?.result).toBe("boom");
    expect(next.segments[1].tool?.status).toBe("running");
    // 非 target 段对象不可变（浅拷贝更新）
    expect(next.segments[1]).toBe(draft.segments[1]);
  });

  it("falls back to name with hiveweave__ prefix stripped, last running match only", () => {
    const two: StreamDraft = {
      assistantId: "a1",
      segments: [
        { type: "tool_call", tool: { tool: "bash", input: {}, status: "running" } },
        { type: "tool_call", tool: { tool: "bash", input: {}, status: "running" } },
      ],
    };
    const next = applyToolResult(two, undefined, "hiveweave__bash", true, "done");
    // 只更新最后一个 running 段（并行同名不写串）
    expect(next.segments[0].tool?.status).toBe("running");
    expect(next.segments[1].tool?.status).toBe("ok");
    expect(next.segments[1].tool?.result).toBe("done");
  });

  it("is idempotent on duplicate tool_result events (finished segments untouched)", () => {
    const once = applyToolResult(draft, "c1", undefined, true, "ok");
    const twice = applyToolResult(once, "c1", "bash", false, "second event");
    // 已完结段不被第二个完成信号覆盖（双广播路径幂等）
    expect(twice.segments[0].tool?.status).toBe("ok");
    expect(twice.segments[0].tool?.result).toBe("ok");
  });

  it("does not mis-write when id is missing but segments have ids", () => {
    // 事件无 id、段全带 id：按名称兜底仅在段无 id 或名称匹配时命中——
    // 此处段名 bash/read_file 与事件名 bash 匹配 → 更新最后一个 bash 段
    const next = applyToolResult(draft, undefined, "bash", true, "r");
    expect(next.segments[1].tool?.status).toBe("ok");
    expect(next.segments[0].tool?.status).toBe("running");
  });

  it("returns the same draft when nothing matches", () => {
    const next = applyToolResult(draft, "nope", undefined, true, "x");
    expect(next).toBe(draft);
  });
});

describe("tryParseToolCalls", () => {
  it("defaults legacy persisted calls to ok (no eternal spinner)", () => {
    const calls = tryParseToolCalls(
      JSON.stringify([{ id: "c1", type: "function", function: { name: "bash", arguments: "{}" } }]),
    );
    expect(calls[0].status).toBe("ok");
  });

  it("maps ok:false (tool_history failure mark) to error", () => {
    const calls = tryParseToolCalls(
      JSON.stringify([{ id: "c1", type: "function", function: { name: "bash", arguments: "{}" }, ok: false }]),
    );
    expect(calls[0].status).toBe("error");
  });
});

describe("nextBadgePopToken", () => {
  it("does not pop on first observation / remount", () => {
    expect(nextBadgePopToken(null, 5, 0)).toEqual({ token: 0, lastSeen: 5 });
  });

  it("pops only when count increments", () => {
    expect(nextBadgePopToken(3, 4, 0)).toEqual({ token: 1, lastSeen: 4 });
    expect(nextBadgePopToken(4, 4, 1)).toEqual({ token: 1, lastSeen: 4 });
    expect(nextBadgePopToken(4, 2, 1)).toEqual({ token: 1, lastSeen: 2 });
  });
});

describe("draftFromStreamingMessage", () => {
  it("omits tool chips when live replay will supply them", () => {
    const draft = draftFromStreamingMessage(
      {
        id: "m1",
        role: "assistant",
        content: "hi",
        timestamp: 1,
        toolCalls: [{ tool: "bash", input: { command: "ls" } }],
        _thinking: "plan",
      },
      { includeTools: false },
    );
    expect(draft.segments.map((s) => s.type)).toEqual(["thinking", "text"]);
  });

  it("copies isBackground onto the draft", () => {
    const draft = draftFromStreamingMessage({
      id: "m1",
      role: "assistant",
      content: "",
      timestamp: 1,
      isBackground: true,
    });
    expect(draft.isBackground).toBe(true);
  });
});

describe("isTeamChannelMessage", () => {
  it("is the letter tray: team rows and background user, not tool-loop assistants", () => {
    expect(
      isTeamChannelMessage({
        id: "a",
        role: "assistant",
        content: "chip",
        timestamp: 1,
        isBackground: true,
      }),
    ).toBe(false);
    expect(
      isTeamChannelMessage({
        id: "u",
        role: "user",
        content: "wake",
        timestamp: 1,
        isBackground: true,
      }),
    ).toBe(true);
    expect(
      isTeamChannelMessage({
        id: "fg",
        role: "assistant",
        content: "hello",
        timestamp: 1,
        isBackground: false,
      }),
    ).toBe(false);
    expect(
      isTeamChannelMessage({
        id: "t",
        role: "team",
        content: "peer",
        timestamp: 1,
      }),
    ).toBe(true);
  });
});

describe("streamEventIsBackground", () => {
  it("defaults true and honors payload flags", () => {
    expect(streamEventIsBackground(undefined)).toBe(true);
    expect(streamEventIsBackground({ is_background: false })).toBe(false);
    expect(streamEventIsBackground({ isBackground: true })).toBe(true);
    expect(streamEventIsBackground({ role: "assistant" })).toBe(true);
  });

  it("returns undefined when the payload has no background flag", () => {
    expect(streamEventBackgroundFlag({ role: "assistant" })).toBeUndefined();
    expect(streamEventBackgroundFlag({ is_background: false })).toBe(false);
    expect(streamEventBackgroundFlag({ is_background: 0 })).toBe(false);
    expect(streamEventBackgroundFlag({ isBackground: 1 })).toBe(true);
  });
});

describe("mergeStreamDraftIntoMessages", () => {
  it("overlays chips onto a background assistant without touching others", () => {
    const msgs = [
      { id: "u1", role: "user" as const, content: "hi", timestamp: 1, isBackground: false },
      { id: "a1", role: "assistant" as const, content: "", timestamp: 2, isBackground: true },
    ];
    const merged = mergeStreamDraftIntoMessages(
      msgs,
      {
        assistantId: "a1",
        isBackground: true,
        segments: [{ type: "tool_call", tool: { tool: "hire_agent", input: {} } }],
      },
      { isStreaming: true },
    );
    expect(merged[0].isBackground).toBe(false);
    expect(merged[1].isBackground).toBe(true);
    expect(merged[1].toolCalls?.map((t) => t.tool)).toEqual(["hire_agent"]);
    expect(merged[1].isStreaming).toBe(true);
  });

  it("does not latch background true when the draft omits the flag", () => {
    const msgs = [
      { id: "a1", role: "assistant" as const, content: "", timestamp: 1, isBackground: false },
    ];
    const merged = mergeStreamDraftIntoMessages(
      msgs,
      { assistantId: "a1", segments: [{ type: "text", content: "hi" }] },
      { isStreaming: true },
    );
    expect(merged[0].isBackground).toBe(false);
    expect(merged[0].content).toBe("hi");
  });

  it("carries round_boundary segments whole and keeps them out of content", () => {
    const msgs = [
      { id: "a1", role: "assistant" as const, content: "", timestamp: 1, isBackground: false },
    ];
    const merged = mergeStreamDraftIntoMessages(
      msgs,
      {
        assistantId: "a1",
        segments: [
          { type: "text", content: "第一轮旁白" },
          { type: "round_boundary", round: 1 },
          { type: "text", content: "第二轮旁白" },
        ],
      },
      { isStreaming: true },
    );
    // _segments 原样携带（渲染统一：live 与持久化同分支）
    expect(merged[0]._segments?.map((s) => s.type)).toEqual([
      "text",
      "round_boundary",
      "text",
    ]);
    expect(merged[0]._segments?.[1]).toEqual({ type: "round_boundary", round: 1 });
    // content 只拼接 text 段——不再有「—— 第 N 轮 ——」标记混入
    expect(merged[0].content).toBe("第一轮旁白第二轮旁白");
  });
});

describe("mapDbToChatMessages", () => {
  it("treats snake_case is_background as team-channel background", () => {
    const mapped = mapDbToChatMessages([
      { id: "d1", role: "user", content: "digest", created_at: 1, is_background: 1 },
      { id: "t1", role: "team", content: "letter", created_at: 2, is_background: 0 },
    ]);
    expect(mapped[0].isBackground).toBe(true);
    expect(mapped[1].isBackground).toBe(false);
    expect(isTeamChannelMessage(mapped[0])).toBe(true);
    expect(isTeamChannelMessage(mapped[1])).toBe(true);
  });

  it("restores thinking + tool_call segments from metadata.segments (DSH block timeline)", () => {
    const mapped = mapDbToChatMessages([
      {
        id: "a1",
        role: "assistant",
        content: "答复",
        metadata: JSON.stringify({
          segments: [
            { type: "thinking", content: "先分析" },
            { type: "text", content: "开始处理" },
            { type: "tool_call", tool: "read_file", id: "c1", input: { path: "a.py" }, status: "ok", result: "body" },
            { type: "thinking", content: "再总结" },
          ],
        }),
      },
    ]);
    expect(mapped[0]._segments?.map((s) => s.type)).toEqual([
      "thinking",
      "text",
      "tool_call",
      "thinking",
    ]);
    expect(mapped[0]._segments?.[0]).toEqual({ type: "thinking", content: "先分析" });
    expect(mapped[0]._segments?.[3]).toEqual({ type: "thinking", content: "再总结" });
    expect(mapped[0]._segments?.[2].tool?.tool).toBe("read_file");
  });

  it("accepts round_boundary segments and passes the round through", () => {
    const mapped = mapDbToChatMessages([
      {
        id: "a1",
        role: "assistant",
        content: "答复",
        metadata: JSON.stringify({
          segments: [
            { type: "text", content: "第一轮" },
            { type: "round_boundary", round: 1 },
            { type: "text", content: "第二轮" },
            { type: "round_boundary" }, // 缺轮号也放行（渲染端兜底文案）
          ],
        }),
      },
    ]);
    expect(mapped[0]._segments).toEqual([
      { type: "text", content: "第一轮" },
      { type: "round_boundary", round: 1 },
      { type: "text", content: "第二轮" },
      { type: "round_boundary" },
    ]);
  });
});

describe("shouldWriteChatCache", () => {
  const team = { id: "t1", role: "team" as const, content: "letter", timestamp: 1 };

  it("does not write the previous person's snapshot into the new agent slot", () => {
    expect(
      shouldWriteChatCache({
        agentId: "B",
        messagesOwnerId: "A",
        persistReady: true,
        next: [team],
        existing: undefined,
      }),
    ).toBe(false);
  });

  it("does not persist a loading-empty list over a populated session", () => {
    expect(
      shouldWriteChatCache({
        agentId: "A",
        messagesOwnerId: "A",
        persistReady: false,
        next: [],
        existing: [team],
      }),
    ).toBe(false);
    expect(
      shouldWriteChatCache({
        agentId: "A",
        messagesOwnerId: "A",
        persistReady: true,
        next: [],
        existing: [team],
      }),
    ).toBe(false);
  });

  it("writes once the loaded transcript belongs to the viewed agent", () => {
    expect(
      shouldWriteChatCache({
        agentId: "A",
        messagesOwnerId: "A",
        persistReady: true,
        next: [team],
        existing: undefined,
      }),
    ).toBe(true);
  });
});

describe("sanitizeMessagesForCache", () => {
  it("drops empty finished assistants and clears streaming flags", () => {
    const next = sanitizeMessagesForCache([
      { id: "t1", role: "team", content: "letter", timestamp: 1, isStreaming: true },
      { id: "a1", role: "assistant", content: "", timestamp: 2, isStreaming: false },
      {
        id: "a2",
        role: "assistant",
        content: "",
        timestamp: 3,
        isStreaming: false,
        toolCalls: [{ tool: "bash", input: {} }],
      },
    ]);
    expect(next.map((m) => m.id)).toEqual(["t1", "a2"]);
    expect(next[0].isStreaming).toBe(false);
    expect(next[1].toolCalls?.[0].tool).toBe("bash");
  });
});


describe("settledMessageHasSegments（八轮收口竞态 fetch-then-swap 判据）", () => {
  const mk = (over: Partial<ChatMessage> & { id: string }): ChatMessage => ({
    role: "assistant",
    content: "",
    timestamp: Date.now(),
    ...over,
  });

  it("带非空 _segments 的消息放行", () => {
    const msgs = [mk({ id: "m1", _segments: [{ type: "text", content: "hi" }] })];
    expect(settledMessageHasSegments(msgs, "m1")).toBe(true);
  });

  it("无 _segments 的中途快照不放行（旁白拼接平文本）", () => {
    const msgs = [mk({ id: "m1", content: "旁白旁白旁白" })];
    expect(settledMessageHasSegments(msgs, "m1")).toBe(false);
    expect(settledMessageHasSegments(msgs, "m1")).toBe(false);
  });

  it("目标消息不存在（未走流式的纯文本路径）放行", () => {
    expect(settledMessageHasSegments([mk({ id: "other" })], "m1")).toBe(true);
    expect(settledMessageHasSegments([], null)).toBe(true);
  });
});

/**
 * P1 富文本（2026-09-06）：附件 ref 合并 gallery、文件 chip 路径/摘要提取。
 */
describe("collectMessageImages（msg.images + attachments image 并一个 gallery）", () => {
  it("images 在前、attachments image 在后，合为单列表（DSH 连续 image 并组语义）", () => {
    const imgs = collectMessageImages({
      images: ["data:image/png;base64,AAA"],
      attachments: [
        { kind: "image", name: "b.png", urlOrId: "https://x/b.png", width: 480, height: 240 },
        { kind: "file", name: "c.pdf", urlOrId: "att_c" },
        { kind: "image", name: "d.png", urlOrId: "data:image/png;base64,BBB" },
      ],
    });
    expect(imgs).toHaveLength(3);
    expect(imgs.map((i) => i.src)).toEqual([
      "data:image/png;base64,AAA",
      "https://x/b.png",
      "data:image/png;base64,BBB",
    ]);
    expect(imgs[1]).toMatchObject({ name: "b.png", width: 480, height: 240 });
  });

  it("attachments 的不透明存储 id（非 URL）跳过 —— 后端句柄解析落地前不渲染碎图", () => {
    const imgs = collectMessageImages({
      attachments: [
        { kind: "image", name: "a.png", urlOrId: "att_123" },
        { kind: "image", name: "b.png", urlOrId: "blob:xyz" },
      ],
    });
    expect(imgs).toHaveLength(1);
    expect(imgs[0].src).toBe("blob:xyz");
  });

  it("空消息/空数组 → 空列表", () => {
    expect(collectMessageImages({})).toEqual([]);
    expect(collectMessageImages({ images: [], attachments: [] })).toEqual([]);
  });
});

describe("collectFileAttachments", () => {
  it("只留 kind===file 的附件", () => {
    const atts = collectFileAttachments({
      attachments: [
        { kind: "image", name: "a.png", urlOrId: "https://x/a.png" },
        { kind: "file", name: "spec.md", urlOrId: "att_1", bytes: 2048 },
      ],
    });
    expect(atts).toHaveLength(1);
    expect(atts[0].name).toBe("spec.md");
  });
});

describe("extractToolFilePath（文件 chip 路径提取）", () => {
  it("write_file/read_file/edit_file：filePath 与 path 别名", () => {
    expect(extractToolFilePath("write_file", { filePath: "src/a.ts" })).toBe("src/a.ts");
    expect(extractToolFilePath("read_file", { path: "docs/b.md" })).toBe("docs/b.md");
    expect(extractToolFilePath("edit_file", { file_path: "lib/c.py" })).toBe("lib/c.py");
  });

  it("apply_patch：patches[] 里取第一个带 filePath 的 op，也支持直挂顶层", () => {
    expect(
      extractToolFilePath("apply_patch", {
        patches: [
          { op: "update", filePath: "src/x.ts", oldString: "a", newString: "b" },
          { op: "add", filePath: "src/y.ts", content: "c" },
        ],
      }),
    ).toBe("src/x.ts");
    expect(
      extractToolFilePath("apply_patch", { filePath: "direct.ts", newString: "b" }),
    ).toBe("direct.ts");
  });

  it("非文件工具 / 提取不到路径 → null（回落标准行）", () => {
    expect(extractToolFilePath("bash", { command: "ls" })).toBeNull();
    expect(extractToolFilePath("apply_patch", { patches: [] })).toBeNull();
    expect(extractToolFilePath("write_file", undefined)).toBeNull();
  });
});

describe("toolResultFirstLine（文件 chip 单行摘要）", () => {
  it("成功取首行（Updated x (+2 lines) 类），失败取 error 首行", () => {
    expect(toolResultFirstLine("Updated src/a.ts (+2 lines)\nsecond\nthird")).toBe(
      "Updated src/a.ts (+2 lines)",
    );
    expect(toolResultFirstLine("FileNotFoundError: no such file\nstack…")).toBe(
      "FileNotFoundError: no such file",
    );
  });

  it("跳过前导空行；全空白 → null；非字符串 → null", () => {
    expect(toolResultFirstLine("\n\n  Created docs/d.md")).toBe("Created docs/d.md");
    expect(toolResultFirstLine("  \n \n")).toBeNull();
    expect(toolResultFirstLine(undefined)).toBeNull();
  });

  it("超长单行按 FILE_CHIP_SUMMARY_MAX 硬截加省略号", () => {
    const long = "x".repeat(300);
    const out = toolResultFirstLine(long)!;
    expect(out.length).toBe(FILE_CHIP_SUMMARY_MAX + 1);
    expect(out.endsWith("…")).toBe(true);
  });
});

describe("mapDbToChatMessages attachments 透传（后端回传待接的 metadata 口）", () => {
  it("metadata.attachments（JSON 字符串）→ 结构化数组，坏条目丢弃", () => {
    const meta = JSON.stringify({
      attachments: [
        { kind: "file", name: "a.md", urlOrId: "att_a", bytes: 10 },
        { kind: "nope", name: "bad", urlOrId: "x" },
        "garbage",
      ],
    });
    const [m] = mapDbToChatMessages([{ id: "m1", role: "assistant", content: "", metadata: meta }]);
    expect(m.attachments).toHaveLength(1);
    expect(m.attachments![0]).toMatchObject({ kind: "file", name: "a.md", urlOrId: "att_a" });
  });

  it("无 metadata / 无 attachments → undefined，不破坏现状", () => {
    const [m] = mapDbToChatMessages([{ id: "m1", role: "assistant", content: "" }]);
    expect(m.attachments).toBeUndefined();
  });
});

describe("fixplan #8 交付状态徽章（后端 metadata.delivery_state → ChatMessage.deliveryBadge）", () => {
  const mapOne = (meta: unknown) =>
    mapDbToChatMessages([
      { id: "m1", role: "assistant", content: "hi", metadata: JSON.stringify(meta) },
    ])[0];

  it("unmarked ⇒ state=unmarked（CEO 未标记时用户看到的那一枚）", () => {
    const m = mapOne({ delivery_state: "unmarked" });
    expect(m.deliveryBadge).toEqual({ state: "unmarked" });
  });

  it("complete ⇒ 带核验时间", () => {
    const m = mapOne({ delivery_state: "complete", delivery_at: "2026-09-14T21:30:00+08:00" });
    expect(m.deliveryBadge).toEqual({
      state: "complete",
      deliveredAt: "2026-09-14T21:30:00+08:00",
    });
  });

  it("blocked ⇒ 带待收口项（只收 message 字段，坏条目丢弃）", () => {
    const m = mapOne({
      delivery_state: "blocked",
      delivery_blockers: [
        { code: "LEDGER_APPROVED_OPEN", message: "1 个 approved 未 closed 任务" },
        { code: "X" },
        "garbage",
      ],
    });
    expect(m.deliveryBadge).toEqual({
      state: "blocked",
      blockers: ["1 个 approved 未 closed 任务"],
    });
  });

  it("无 metadata / 无该字段 ⇒ undefined（不破坏现状）", () => {
    expect(mapOne({}).deliveryBadge).toBeUndefined();
    expect(mapOne({ delivery_state: "not-a-state" }).deliveryBadge).toBeUndefined();
    expect(mapOne({ delivery_state: 123 }).deliveryBadge).toBeUndefined();
    const noMeta = mapDbToChatMessages([{ id: "m2", role: "assistant", content: "x" }])[0];
    expect(noMeta.deliveryBadge).toBeUndefined();
  });

  it("★ 徽章与正文措辞无关（这正是它取代 8 词门禁的理由）", () => {
    const phrases = [
      "全部完成",
      "All done",
      "Terminé",
      "记录之三（不做完工判断）",
      "我不宣称全部完成",
    ];
    const badges = phrases.map((p) => {
      const m = mapDbToChatMessages([
        {
          id: "m3",
          role: "assistant",
          content: p,
          metadata: JSON.stringify({ delivery_state: "unmarked" }),
        },
      ])[0];
      return JSON.stringify(m.deliveryBadge);
    });
    expect(new Set(badges).size).toBe(1);
  });
});
