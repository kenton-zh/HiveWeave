import { describe, it, expect } from "vitest";
import {
  composerSendLabel,
  ENQUEUE_HINT,
  INSERT_HINT,
  STOP_LABEL,
} from "./constants";

/**
 * FE-13/FE-19 输入区微文案契约（前端设计规格 §10.1.5 发送状态矩阵 +
 * 方案 §4.7 微文案表）。文案函数化 = UI 渲染与测试同源，防回退：
 * - 忙线点击「发送」实际入队（useChatSend）⇒ 按钮必须叫「加入队列」；
 * - 「插入」→「插话」，且带生效时机说明（插话 ≠ 排队、不承诺打断工具）；
 * - 「停止」→「停止本轮」（点名作用对象 = 本轮执行轮次，§10.1.6）。
 */
describe("chat 输入区微文案（FE-13 §10.1.5 / FE-19 方案 §4.7）", () => {
  it("空闲主操作 =「发送」；忙线 =「加入队列」", () => {
    expect(composerSendLabel(false)).toBe("发送");
    expect(composerSendLabel(true)).toBe("加入队列");
  });

  it("加入队列有生效时机说明；插话 ≠ 排队且说明生效时机", () => {
    expect(ENQUEUE_HINT).toContain("当前回复完成后自动发送");
    // 「插话」语义三点：尽快进入时机 / 不等待完成 / 不承诺打断工具
    expect(INSERT_HINT).toContain("插话");
    expect(INSERT_HINT).toContain("当前轮次可接收输入的时机");
    expect(INSERT_HINT).toContain("不承诺打断");
  });

  it("停止按钮相位文案：静默态点名「本轮」，收口态只来自后端确认语义", () => {
    expect(STOP_LABEL.none).toBe("停止本轮");
    expect(STOP_LABEL.stopping).toBe("停止中…");
    expect(STOP_LABEL.uncertain).toBe("停止未确认，重试");
    expect(STOP_LABEL.stopped).toBe("已停止");
  });
});
