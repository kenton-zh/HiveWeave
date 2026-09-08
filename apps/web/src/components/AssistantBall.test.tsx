/**
 * AssistantBall — 面板只承载助理对话（用户 2026-09-08 拍板去 CEO）。
 * 断言：/api/ball/state 的 projects[].ceo 分支不被消费——面板标题
 * 只出助理，项目 CEO 条目不渲染为任何可点目标。
 */
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../api/shared", () => ({
  getApiKey: vi.fn(() => ""),
}));

import AssistantBall from "./AssistantBall";

function jsonResp(data: unknown) {
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve(data),
  });
}

describe("AssistantBall 只显示助理（无 CEO 标签）", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("消费 assistant 分支，忽略 projects[].ceo", async () => {
    const fetchMock = vi.fn((input: string | URL | Request) => {
      const url = String(input);
      if (url.includes("/api/ball/state")) {
        return jsonResp({
          assistant: { agentId: "assist-1", name: "小蜂", unread: 2 },
          projects: [
            { name: "office-godot", ceo: { agentId: "ceo-1", name: "归零", unread: 9 } },
            { name: "另一个项目", ceo: { agentId: "ceo-2", name: "其他CEO", unread: 1 } },
          ],
        });
      }
      return jsonResp({ messages: [] });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    render(<AssistantBall />);

    // 球态徽章只累计助理未读（=2），不含 CEO 的 9+1
    await waitFor(() => {
      expect(screen.getByText("2")).toBeTruthy();
    });
    expect(screen.queryByText("12")).toBeNull();

    // 点球展开面板，头部标题只出助理
    fireEvent.click(screen.getByTitle("HiveWeave 助理（可拖动）"));
    await waitFor(() => {
      expect(screen.getByText("小蜂")).toBeTruthy();
    });
    // CEO 条目完全不渲染
    expect(screen.queryByText(/office-godot/)).toBeNull();
    expect(screen.queryByText(/归零/)).toBeNull();
    expect(screen.queryByText(/其他CEO/)).toBeNull();
  });
});
