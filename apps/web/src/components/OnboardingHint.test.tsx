import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import {
  OnboardingHint,
  ONBOARDING_STORAGE_KEY,
  ONBOARDING_STEPS,
  isOnboardingDismissed,
} from "./OnboardingHint";

/**
 * FE-19 新手引导（前端设计规格 §8.8）验收：
 * - 只教三件事（三步文案齐全，一步一件事）；
 * - 可跳过（跳过/完成都持久化 localStorage，之后不再自动弹）；
 * - 完成后可从帮助入口重开；
 * - 非模态：卡片不带 aria-modal，不挡操作。
 */

beforeEach(() => {
  localStorage.clear();
});

describe("OnboardingHint 三步引导（§8.8 只教三件事）", () => {
  it("首次进入自动出现第一步，三步文案齐全（运行状态/发消息/待我处理）", () => {
    render(<OnboardingHint />);
    expect(screen.getByTestId("onboarding-card")).toBeInTheDocument();
    expect(screen.getByText(ONBOARDING_STEPS[0].title)).toBeInTheDocument();
    // 步数指示 1 / 3
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    // 三件事的关键词都出现在三步文案里（顶栏上班/组织树/待我处理）
    const allBodies = ONBOARDING_STEPS.map((s) => s.body).join("\n");
    expect(allBodies).toContain("上班");
    expect(allBodies).toContain("组织树");
    expect(allBodies).toContain("待我处理");
  });

  it("下一步推进到第二步、第三步；最后一步显示「完成」", () => {
    render(<OnboardingHint />);
    fireEvent.click(screen.getByTestId("onboarding-next"));
    expect(screen.getByText("2 / 3")).toBeInTheDocument();
    expect(screen.getByText(ONBOARDING_STEPS[1].title)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("onboarding-next"));
    expect(screen.getByText("3 / 3")).toBeInTheDocument();
    expect(screen.getByText(ONBOARDING_STEPS[2].title)).toBeInTheDocument();
    expect(screen.getByTestId("onboarding-finish")).toBeInTheDocument();
    // 上一步可回退
    fireEvent.click(screen.getByText("上一步"));
    expect(screen.getByText("2 / 3")).toBeInTheDocument();
  });

  it("卡片非模态：不声明 aria-modal、不出现 dialog 角色（不挡操作）", () => {
    render(<OnboardingHint />);
    expect(screen.getByRole("region", { name: "新手引导" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("onboarding-card").getAttribute("aria-modal")).toBeNull();
  });
});

describe("OnboardingHint 跳过与持久化", () => {
  it("点「跳过引导」：写 localStorage 并收起，收起后只剩重开入口", () => {
    render(<OnboardingHint />);
    fireEvent.click(screen.getByTestId("onboarding-skip"));
    expect(screen.queryByTestId("onboarding-card")).toBeNull();
    expect(screen.getByTestId("onboarding-reopen")).toBeInTheDocument();
    expect(localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBe("1");
    expect(isOnboardingDismissed()).toBe(true);
  });

  it("点「完成」：同样持久化并收起", () => {
    render(<OnboardingHint />);
    fireEvent.click(screen.getByTestId("onboarding-next"));
    fireEvent.click(screen.getByTestId("onboarding-next"));
    fireEvent.click(screen.getByTestId("onboarding-finish"));
    expect(screen.queryByTestId("onboarding-card")).toBeNull();
    expect(localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBe("1");
  });

  it("已读后重挂载不再自动弹出（localStorage 记忆）", () => {
    localStorage.setItem(ONBOARDING_STORAGE_KEY, "1");
    render(<OnboardingHint />);
    expect(screen.queryByTestId("onboarding-card")).toBeNull();
    expect(screen.getByTestId("onboarding-reopen")).toBeInTheDocument();
  });
});

describe("OnboardingHint 帮助入口重开", () => {
  it("点重开入口重新打开引导，从第一步开始", () => {
    localStorage.setItem(ONBOARDING_STORAGE_KEY, "1");
    render(<OnboardingHint />);
    fireEvent.click(screen.getByTestId("onboarding-reopen"));
    expect(screen.getByTestId("onboarding-card")).toBeInTheDocument();
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
  });

  it("重开后关闭（×）同样持久化", () => {
    render(<OnboardingHint />);
    fireEvent.click(screen.getByRole("button", { name: "关闭新手引导" }));
    expect(screen.queryByTestId("onboarding-card")).toBeNull();
    expect(localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBe("1");
  });
});
