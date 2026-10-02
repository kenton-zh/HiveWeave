import { describe, it, expect } from "vitest";
import { digestPreview, digestRoleLabel, digestStatusLabel, parseSystemDigest } from "./systemDigest";

const MESSAGES_BLOCK =
  "## Messages (chronological) — JSON lines; reply_required=true means you must message that sender before done_slice\n" +
  JSON.stringify({ id: "dc8f81a0", from: "system", content: "[TASK CLOSED] 设计稿\n微缩小镇", reply_required: false }) +
  "\n" +
  JSON.stringify({ id: "5289169a", from: "天线", content: "需要你更正", reply_required: true, task_id: "bf7e1ae9" });

describe("parseSystemDigest", () => {
  it("解析 Messages 块：每条 JSON 行一个条目，字段映射到位", () => {
    const d = parseSystemDigest(MESSAGES_BLOCK);
    expect(d).not.toBeNull();
    expect(d!.sections).toHaveLength(1);
    const s = d!.sections[0];
    expect(s.title).toBe("来信");
    expect(s.entries).toHaveLength(2);
    expect(s.entries[0].from).toBe("system");
    // 内嵌 \n 转义还原为真换行
    expect(s.entries[0].content).toBe("[TASK CLOSED] 设计稿\n微缩小镇");
    expect(s.entries[0].replyRequired).toBe(false);
    expect(s.entries[1].replyRequired).toBe(true);
    expect(s.entries[1].taskId).toBe("bf7e1ae9");
    // LLM 说明内嵌在标题行 " — " 之后，人读标题已剥掉它
    expect(s.notes).toEqual([]);
  });

  it("多块 digest（Pending + Background）各自成节", () => {
    const content =
      "## Pending Tasks — each line is a JSON object\n" +
      JSON.stringify({ from: "甲", task: "写模块", status: "pending", report_required: true }) +
      "\n## Background updates — 同事进度/回执（仅供参考）\n" +
      JSON.stringify({ id: "abc", from: "乙", content: "进度正常" });
    const d = parseSystemDigest(content);
    expect(d!.sections.map((s) => s.title)).toEqual(["待办任务", "同事动态 · 仅供参考"]);
    expect(d!.sections[0].entries[0].content).toBe("写模块"); // task → content
    expect(d!.sections[0].entries[0].reportRequired).toBe(true);
  });

  it("WORK REJECTED 块：status 映射中文", () => {
    const d = parseSystemDigest(
      "## WORK REJECTED — Rework Required\n" +
        JSON.stringify({ from: "兰屿", status: "rejected", content: "缺测试" }) +
        "\nYou must fix the issues and call submit_task again after fixing.",
    );
    expect(d!.sections[0].title).toBe("返工要求");
    expect(digestStatusLabel(d!.sections[0].entries[0].status!)).toBe("已驳回");
    expect(d!.sections[0].notes.join(" ")).toContain("submit_task");
  });

  it("非 digest（纯文本 / 不以 ## 开头）返回 null → 调用方回退原文", () => {
    expect(parseSystemDigest("上下文已压缩，保留最近 10 轮")).toBeNull();
    expect(parseSystemDigest("")).toBeNull();
    expect(parseSystemDigest(undefined)).toBeNull();
    expect(parseSystemDigest("## 无 JSON 行的标题")).toBeNull(); // 无条目
  });

  it("坏 JSON 行整体回退（不半渲染）", () => {
    expect(parseSystemDigest("## Messages\n{not json}\n" + JSON.stringify({ from: "甲" }))).toBeNull();
  });

  it("JSON 数组行回退（非对象行 → 无有效条目 → 整体回退）", () => {
    expect(parseSystemDigest("## Messages\n[1,2,3]")).toBeNull();
    // 数组行不以 { 开头 ⇒ 归 notes，不影响有效条目
    expect(parseSystemDigest('## Messages\n{"from": "甲"}\n[1,2,3]')).not.toBeNull();
  });

  it("Open Work 块：title 作正文、you_are 角色、指令行归 notes", () => {
    const d = parseSystemDigest(
      "## Open Work — 你名下有未收口的开放工作（沉默看门狗穿透唤醒；与 inbox 读态无关）\n" +
        "继续推进或用 commit_turn 正确声明状态；完成后 submit_task / 解除阻塞清账。\n" +
        JSON.stringify({ task_id: "ab12cd34", title: "实现接缝", status: "in_progress", you_are: "assignee" }),
    );
    expect(d).not.toBeNull();
    const s = d!.sections[0];
    expect(s.title).toBe("未收口的开放工作");
    expect(s.notes.join(" ")).toContain("commit_turn");
    expect(s.entries[0].content).toBe("实现接缝");
    expect(s.entries[0].youAre).toBe("assignee");
    expect(s.entries[0].taskId).toBe("ab12cd34");
  });

  it("Pending Review 块：reviewer 角色 + approved 状态", () => {
    const d = parseSystemDigest(
      "## Pending Review — 名下任务待办（ledger 义务，与 inbox 读态无关）\n" +
        "Use review_task(taskId, decision='approve'/'rework') to review.\n" +
        JSON.stringify({ task_id: "ff00", title: "审计批次", status: "approved", you_are: "reviewer" }),
    );
    const e = d!.sections[0].entries[0];
    expect(e.content).toBe("审计批次");
    expect(digestStatusLabel(e.status!)).toBe("已通过");
    expect(digestRoleLabel(e.youAre!)).toBe("我是审查人");
  });

  it("Goals Workbook 块：单条目、标题无破折号、正文含真换行", () => {
    const d = parseSystemDigest(
      "## Goals Workbook (updated)\n" +
        JSON.stringify({ from: "工作簿更新", content: "Objective: 上线\nKey Results:\n  - [done] A" }),
    );
    const s = d!.sections[0];
    expect(s.title).toBe("目标工作簿更新");
    expect(s.entries[0].content).toContain("\n  - [done] A");
  });

  it("digestPreview 折叠摘要含块名与条数", () => {
    const d = parseSystemDigest(MESSAGES_BLOCK)!;
    const p = digestPreview(d);
    expect(p).toContain("来信 2 条");
    expect(p).toContain("system");
    expect(p).toContain("天线");
  });
});
