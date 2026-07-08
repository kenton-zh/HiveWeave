import { useState } from "react";
import { useAppStore } from "../store.js";

interface Props {
  ceoAgentId: string;
  onClose: () => void;
}

// Workflow: existing project with code
const ANALYZE_AND_BUILD = `请按以下流程启动项目：

1. 探索: read_file/list_files/grep 分析现有代码库、技术栈、模块结构
2. 环境: 和架构师讨论后创建 .hiveweave/env.sh（venv/PATH/Docker等）
3. 架构: 设计组织架构，写入 charter，指示 HR 按架构招人
4. 验证: 招人后先让全员测试工具是否可用，有问题反馈给我再开发`;

// Workflow: greenfield / brainstorm first
const BRAINSTORM = `这是一个新项目，目前工作区为空。先和我讨论以下问题，不要直接动手：
- 项目目标和范围
- 技术栈偏好（Python/Node/Rust/Go？）
- 环境配置要求（Docker？venv？特定版本？）
- 团队规模和角色需求
问清楚以上所有问题后，再设计架构并招人。`;

// Workflow: quick prototype (solo, no org)
const QUICK_PROTOTYPE = `快速原型模式。不需要组建团队，你一个人完成：
1. 探索工作区，了解项目需求
2. 创建 .hiveweave/env.sh
3. 直接写代码，不需要招人、charter
4. 完成后汇报结果`;

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
              探索代码库 → 配置环境 → 设计架构 → 招人 → 工具测试
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
              CEO 先和你讨论目标、技术栈、环境要求，确认后再搭建团队
            </div>
          </button>

          <button
            onClick={() => handleSend(QUICK_PROTOTYPE)}
            className="w-full text-left px-4 py-3 rounded-lg border border-g-border bg-g-bg-soft hover:bg-g-bg-soft transition-colors"
          >
            <div className="text-g-fg font-medium">
              快速原型 — 一个人直接写
            </div>
            <div className="text-g-fg-3 text-sm mt-0.5">
              不招人、不建组织，CEO 独自完成，适合小任务或探索性工作
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
