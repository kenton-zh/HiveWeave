import { useState } from "react";
import { useAppStore } from "../store.js";

interface Props {
  ceoAgentId: string;
  onClose: () => void;
}

// Workflow: existing project with code
const ANALYZE_AND_BUILD = `请按以下流程启动项目（工程协作，不是闲聊；每轮结束必须 commit_turn）：

1. EXPLORE：用 list_files / read_file / grep 摸清代码库、技术栈、模块边界与完成度；用 update_goals 写入项目目标与当前焦点。
2. 环境：与架构师（若已招）或自行确认运行方式，需要时创建 .hiveweave/env.sh（venv/PATH/Docker 等）。
3. 组织：选定组织范式，save_charter（只定范式与领域，不定工程师人头）。向 HR 发招聘请求招 manager（coordinator）；工程师人数由 manager 按「一人一模块」拆完再招。executor 的 role 必须带模块名（如「签到排行榜工程师」），禁止一排「前端工程师」。
4. 就位检查（短）：用 ask_agent 让关键角色各做一次工具烟测（读一文件 / 列目录即可），对方须 send_message/ask_agent 回你；不要空等。烟测通过或可接受后立刻进入派活。
5. 派活：按模块 create_task + dispatch_task（现在就要做就直接 dispatch；先写细再派就 create→dispatch(taskId=…)）。走 DEFINE→BUILD→VERIFY→REVIEW→SHIP；审批用 review_task，合并走 git_worktree_merge。
6. 你自己不写业务代码；用 commit_turn 声明每轮状态（in_progress / waiting / blocked / done_slice）。`;

// Workflow: greenfield / brainstorm first
const BRAINSTORM = `这是空工作区上的新项目。先和用户对齐，再招人动手（每轮结束必须 commit_turn）：

先用 question 或 send_message(recipients=["用户"]) 问清并确认：
- 项目目标与成功标准（做到什么算完成）
- 技术栈与约束（语言/框架/平台）
- 环境要求（Docker / 本地 / 特定版本）
- 范围边界（做什么、明确不做什么）
- 团队形态偏好（你一人管 + 少量工程师，还是多层架构）

未得到用户确认前：不要 hire、不要写业务代码、不要大范围改仓库。
确认后：update_goals → save_charter → 指示 HR 招聘（manager 的 role 带领域；executor 的 role 带模块名）→ create_task + dispatch_task 开工，走 DEFINE→SHIP。
要人回复用 ask_agent；单向同步用 notify_agent。`;

// Workflow: quick prototype (minimal org)
const QUICK_PROTOTYPE = `快速原型模式（最小协作，仍须可观测状态；每轮结束必须 commit_turn）：

1. 用 list_files / read_file 弄清工作区与目标；update_goals 写清本原型要交付什么。
2. 需要时创建 .hiveweave/env.sh。
3. 组织保持最小：向 HR 招 1 名全能 executor（role 用具体交付名，如「原型工程师」或带模块名），挂在你名下；不要铺多层架构。
4. 用 create_task + dispatch_task 把活派给该工程师；你负责验收（review_task）与方向，不亲自写业务代码。
5. 跳过冗长 charter/全员工具演练；有阻塞用 ask_agent 问用户或工程师，并用 commit_turn(phase=waiting|blocked, waiting_on=…) 登记等待。
6. 原型可用后 notify_agent/send_message 向用户汇报结果与已知限制。`;

export default function NewProjectDialog({ ceoAgentId, onClose }: Props) {
  const [customInput, setCustomInput] = useState("");
  const setSelectedAgent = useAppStore((s) => s.setSelectedAgent);
  const setRightPanelTab = useAppStore((s) => s.setRightPanelTab);
  const setPendingInitialMessage = useAppStore((s) => s.setPendingInitialMessage);

  const handleSend = (message: string) => {
    setSelectedAgent(ceoAgentId);
    setRightPanelTab("chat");
    setPendingInitialMessage({ agentId: ceoAgentId, message });
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="bg-g-bg border border-g-border rounded-xl shadow-2xl p-6 w-full max-w-lg mx-4"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-g-fg">
            项目已创建 — 选择开局方式
          </h2>
          <button
            onClick={onClose}
            className="text-g-fg-3 hover:text-g-fg text-xl leading-none transition-colors"
          >
            ✕
          </button>
        </div>

        {/* Options */}
        <div className="space-y-3 mb-4">
          <button
            onClick={() => handleSend(ANALYZE_AND_BUILD)}
            className="w-full text-left px-4 py-3 rounded-lg border border-g-border bg-g-bg-soft hover:bg-g-bg-soft transition-colors"
          >
            <div className="text-g-fg font-medium">
              已有代码 — 分析项目并搭建团队
            </div>
            <div className="text-g-fg-3 text-sm mt-0.5">
              探索 → 环境 → 按模块招人 → 短烟测 → dispatch 开工
            </div>
          </button>

          <button
            onClick={() => handleSend(BRAINSTORM)}
            className="w-full text-left px-4 py-3 rounded-lg border border-g-border bg-g-bg-soft hover:bg-g-bg-soft transition-colors"
          >
            <div className="text-g-fg font-medium">
              空项目 — 先讨论再动手
            </div>
            <div className="text-g-fg-3 text-sm mt-0.5">
              先对齐目标/技术栈/范围，确认后再招人派活
            </div>
          </button>

          <button
            onClick={() => handleSend(QUICK_PROTOTYPE)}
            className="w-full text-left px-4 py-3 rounded-lg border border-g-border bg-g-bg-soft hover:bg-g-bg-soft transition-colors"
          >
            <div className="text-g-fg font-medium">
              快速原型 — 最小团队交付
            </div>
            <div className="text-g-fg-3 text-sm mt-0.5">
              只招 1 名工程师，CEO 派活验收，适合小任务
            </div>
          </button>
        </div>

        {/* Divider */}
        <div className="flex items-center gap-3 mb-4">
          <div className="flex-1 h-px bg-g-border" />
          <span className="text-g-fg-4 text-sm">或者自定义</span>
          <div className="flex-1 h-px bg-g-border" />
        </div>

        {/* Custom input */}
        <div className="flex gap-2">
          <input
            type="text"
            value={customInput}
            onChange={(e) => setCustomInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && customInput.trim()) {
                handleSend(customInput.trim());
              }
            }}
            placeholder="自己写开局指令…"
            className="flex-1 px-3 py-2 bg-g-bg-soft border border-g-border rounded-lg text-g-fg placeholder-g-fg-4/60 focus:outline-none focus:border-g-blue text-sm"

          />
          <button
            disabled={!customInput.trim()}
            onClick={() => customInput.trim() && handleSend(customInput.trim())}
            className="px-4 py-2 bg-g-blue text-white hover:bg-blue-600 disabled:opacity-40 text-white rounded-lg text-sm font-medium transition-colors"
          >
            发送
          </button>
        </div>
      </div>
    </div>
  );
}
