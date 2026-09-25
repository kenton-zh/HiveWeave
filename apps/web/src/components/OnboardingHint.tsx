import { useEffect, useState } from "react";

/**
 * OnboardingHint —— 新手引导（FE-19 · 前端设计规格 §8.8）。
 *
 * **只教三件事**（§8.8 铁律：⛔ 不要求用户点十几步才能开始使用）：
 *   ① 在哪看项目是否运行（顶栏 ProjectRunControls 的「上班 / 下班」）；
 *   ② 怎么选成员发消息（点场景小人 / 组织树成员 → 聊天窗）；
 *   ③ 在哪看待我处理的决定与交付（场景 HUD「待我处理」入口 —— FE-14 落地，
 *      此处只做文案指引）。
 *
 * 行为契约：
 * - 首次进入自动出现一次；「跳过引导」与「完成」都算已读，写 localStorage
 *   持久记忆，之后不再自动弹出；
 * - 关闭后左下角保留一个小入口（帮助入口），可随时重开；
 * - 非模态：卡片是场景左下角的浮层，不遮全屏、不囚禁焦点、不打断操作
 *   （§8.8「不挡操作」；不声明 aria-modal，见 §8.2 游戏浮窗层 R4-a）。
 */

export const ONBOARDING_STORAGE_KEY = "hiveweave_onboarding_done";

/** 引导是否已被跳过/完成（存储异常一律视为未读过，宁可多提示一次）。 */
export function isOnboardingDismissed(): boolean {
  try {
    return localStorage.getItem(ONBOARDING_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function dismissOnboarding(): void {
  try {
    localStorage.setItem(ONBOARDING_STORAGE_KEY, "1");
  } catch {
    /* 隐私模式 / 存储不可用：本次会话内由组件状态兜底 */
  }
}

/** 三步文案（§8.8 ①②③，一步一件事，不展开教程）。 */
export const ONBOARDING_STEPS: ReadonlyArray<{ title: string; body: string }> = [
  {
    title: "项目在运行吗？",
    body: "看顶栏的「上班 / 下班」按钮：绿点亮起 = 项目运行中，成员们正在干活；灰点 = 已下班，不会派新任务。",
  },
  {
    title: "怎么给成员发消息？",
    body: "点击办公室场景里的小人（或在左侧组织树选成员），聊天窗打开后输入消息发送即可。",
  },
  {
    title: "待我处理的事在哪看？",
    body: "成员的提问、审批请求和交付验收会集中在场景的「待我处理」入口，点开即可逐条处理，不会漏。",
  },
];

export function OnboardingHint({
  onOpenChange,
}: {
  /** 开合回调：宿主（OfficeView）据此时刻周边氛围提示，避免重叠。 */
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(() => !isOnboardingDismissed());
  const [step, setStep] = useState(0);

  useEffect(() => {
    onOpenChange?.(open);
  }, [open, onOpenChange]);

  const close = () => {
    dismissOnboarding();
    setOpen(false);
  };
  const reopen = () => {
    setStep(0);
    setOpen(true);
  };

  if (!open) {
    // 关闭态 = 帮助重开入口（§8.8「可重新打开」）：一枚轻量小药丸。
    return (
      <button
        type="button"
        data-testid="onboarding-reopen"
        onClick={reopen}
        title="新手引导"
        aria-label="打开新手引导"
        className="flex items-center gap-1.5 rounded-full bg-white/65 backdrop-blur-sm border border-g-border px-2.5 py-1 shadow-gm-sm text-[10px] text-g-fg-3 hover:bg-white/90 hover:text-g-fg transition-colors"
      >
        <span
          className="w-3.5 h-3.5 rounded-full bg-g-blue text-white text-[9px] font-bold flex items-center justify-center shrink-0"
          aria-hidden="true"
        >
          ?
        </span>
        新手引导
      </button>
    );
  }

  const current = ONBOARDING_STEPS[step];
  const isLast = step === ONBOARDING_STEPS.length - 1;
  return (
    <div
      data-testid="onboarding-card"
      role="region"
      aria-label="新手引导"
      // 不用 hw-msg-in：那套 keyframes 只由 ChatPanel 的 ChatMotionStyles 注入，
      // 本组件挂在办公室场景（OfficeView）下 —— 引了也没有动画，纯死类。
      className="absolute bottom-0 left-0 z-20 w-[20rem] max-w-[calc(100vw-2rem)] rounded-gmLg border border-g-border bg-white shadow-gm-md p-3.5"
    >
      <div className="flex items-center gap-2 mb-1.5">
        <span className="text-xs font-semibold text-g-fg">新手引导</span>
        <span className="text-[10px] text-g-fg-4 font-mono">
          {step + 1} / {ONBOARDING_STEPS.length}
        </span>
        {/* 进度点：位置感一眼可见，三步引导不需要进度条 */}
        <span className="flex items-center gap-1 ml-1" aria-hidden="true">
          {ONBOARDING_STEPS.map((_, i) => (
            <span
              key={i}
              className={`w-1.5 h-1.5 rounded-full ${i === step ? "bg-g-blue" : "bg-g-border"}`}
            />
          ))}
        </span>
        <button
          type="button"
          onClick={close}
          aria-label="关闭新手引导"
          title="关闭"
          className="ml-auto w-5 h-5 rounded-gm flex items-center justify-center text-g-fg-4 hover:text-g-fg hover:bg-g-bg-muted/70 transition-colors shrink-0"
        >
          <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5} aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
      {/* key=step：换步时内容节点重建，让变更可感知（不加新动效令牌） */}
      <div key={step}>
        <p className="text-sm font-semibold text-g-fg mb-1">{current.title}</p>
        <p className="text-xs text-g-fg-3 leading-relaxed">{current.body}</p>
      </div>
      <div className="flex items-center gap-1.5 mt-2.5">
        <button
          type="button"
          onClick={close}
          data-testid="onboarding-skip"
          className="text-[11px] text-g-fg-4 hover:text-g-fg-2 transition-colors px-1 py-1"
        >
          跳过引导
        </button>
        <span className="flex-1" />
        {step > 0 && (
          <button
            type="button"
            onClick={() => setStep(step - 1)}
            className="text-[11px] px-2.5 py-1 rounded-gm border border-g-border text-g-fg-3 hover:text-g-fg hover:border-g-border-strong transition-colors"
          >
            上一步
          </button>
        )}
        {isLast ? (
          <button
            type="button"
            onClick={close}
            data-testid="onboarding-finish"
            className="text-[11px] px-3 py-1 rounded-gm bg-g-blue text-white font-medium hover:opacity-90 transition-opacity"
          >
            完成
          </button>
        ) : (
          <button
            type="button"
            onClick={() => setStep(step + 1)}
            data-testid="onboarding-next"
            className="text-[11px] px-3 py-1 rounded-gm bg-g-blue text-white font-medium hover:opacity-90 transition-opacity"
          >
            下一步
          </button>
        )}
      </div>
    </div>
  );
}

export default OnboardingHint;
