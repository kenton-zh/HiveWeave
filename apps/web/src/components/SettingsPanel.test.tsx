import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

const upsertSetting = vi.fn();

vi.mock("../api", () => ({
  getMcpServers: vi.fn(async () => []),
  addMcpServer: vi.fn(),
  removeMcpServer: vi.fn(),
  getProjects: vi.fn(async () => [
    { id: "p1", name: "测试项目甲" },
    { id: "p2", name: "测试项目乙" },
  ]),
  getSettings: vi.fn(async () => ({
    settings: [{ key: "unattended_mode:p1", value: "true" }],
  })),
  upsertSetting: (...args: unknown[]) => upsertSetting(...args),
}));

import SettingsPanel from "./SettingsPanel";

describe("SettingsPanel 无人值守分区", () => {
  beforeEach(() => {
    upsertSetting.mockReset();
    upsertSetting.mockResolvedValue({ ok: true });
  });

  it("按项目渲染开关并回显已存状态", async () => {
    render(<SettingsPanel onClose={() => {}} />);
    const rows = await screen.findAllByLabelText(/无人值守 /);
    expect(rows.length).toBe(2);
    // p1 已开（settings 里有 unattended_mode:p1=true）→ aria-pressed 语义用类名判断：
    // 开=bg-g-green-vivid，关=bg-g-border
    expect(rows[0].className).toContain("bg-g-green-vivid");
    expect(rows[1].className).toContain("bg-g-border");
  });

  it("点击关闭态开关 → upsert unattended_mode:<id>=true", async () => {
    render(<SettingsPanel onClose={() => {}} />);
    const rows = await screen.findAllByLabelText(/无人值守 /);
    fireEvent.click(rows[1]);
    await waitFor(() =>
      expect(upsertSetting).toHaveBeenCalledWith(
        "unattended_mode:p2",
        "true",
      ),
    );
  });

  it("点击开启态开关 → upsert false（显式关，读取端识别 false 为有人值守）", async () => {
    render(<SettingsPanel onClose={() => {}} />);
    const rows = await screen.findAllByLabelText(/无人值守 /);
    fireEvent.click(rows[0]);
    await waitFor(() =>
      expect(upsertSetting).toHaveBeenCalledWith(
        "unattended_mode:p1",
        "false",
      ),
    );
  });

  it("upsert 失败 → 回滚 UI 状态", async () => {
    upsertSetting.mockRejectedValue(new Error("boom"));
    render(<SettingsPanel onClose={() => {}} />);
    const rows = await screen.findAllByLabelText(/无人值守 /);
    fireEvent.click(rows[0]);
    await waitFor(() =>
      expect(rows[0].className).toContain("bg-g-green-vivid"),
    );
    expect(screen.getByText(/保存失败/)).toBeTruthy();
  });
});
