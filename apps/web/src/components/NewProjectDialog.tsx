import { useState } from "react";
import { useAppStore } from "../store.js";

interface Props {
  ceoAgentId: string;
  onClose: () => void;
}

const ANALYZE_AND_BUILD = `请按以下流程启动项目：

Phase 0 — 探索：用 read_file / list_files / grep 分析项目代码库和技术栈。
Phase 0.3 — 环境：创建 .hiveweave/env.sh 声明项目开发环境（和架构师讨论后决定）。
Phase 1 — 架构：设计组织架构，写入 charter，指示 HR 按架构招人。
Phase 2 — 验证：招人后先让全员测试工具是否可用，有问题反馈给我，不要直接开发。`;

const BRAINSTORM = `我们刚创建了一个新项目。在搭建团队之前，我想先和你聊聊——这个项目的目标是什么，优先级怎么排，环境配置有什么特殊要求（Docker？venv？Node 版本？）。你也可以问我一些问题来帮我理清思路。`;

export default function NewProjectDialog({ ceoAgentId, onClose }: Props) {
  const [customInput, setCustomInput] = useState("");
  const setSelectedAgent = useAppStore((s) => s.setSelectedAgent);
  const setRightPanelTab = useAppStore((s) => s.setRightPanelTab);
  const setPendingInitialMessage = useAppStore((s) => s.setPendingInitialMessage);

  const handleSend = (message: string) => {
    // Switch to CEO chat panel
    setSelectedAgent(ceoAgentId);
    setRightPanelTab("chat");
    // Store the message for ChatPanel to send on mount — this ensures
    // the WebSocket event handler is properly registered by ChatPanel
    // before the chat is pushed, so streaming events are not lost.
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
            项目已创建 — 接下来做什么？
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
              让 CEO 分析项目并搭建组织架构
            </div>
            <div className="text-g-fg-3 text-sm mt-0.5">
              CEO 分析代码库 → 配置环境 → 设计组织架构 → HR 招人 → 全员工具测试
            </div>
          </button>

          <button

            onClick={() => handleSend(BRAINSTORM)}
            className="w-full text-left px-4 py-3 rounded-lg border border-g-border bg-g-bg-soft hover:bg-g-bg-soft transition-colors"
          >
            <div className="text-g-fg font-medium">
              与 CEO 讨论项目方向
            </div>
            <div className="text-g-fg-3 text-sm mt-0.5">
              在搭建团队之前，先和 CEO 聊聊目标、环境要求、技术约束和注意事项
            </div>
          </button>
        </div>

        {/* Divider */}
        <div className="flex items-center gap-3 mb-4">
          <div className="flex-1 h-px bg-g-border" />
          <span className="text-g-fg-4 text-sm">或者</span>
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
            placeholder="输入你想让 CEO 做的事情…"
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
