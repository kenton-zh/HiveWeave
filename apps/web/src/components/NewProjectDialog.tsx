import { useState } from "react";
import { useAppStore } from "../store.js";

interface Props {
  ceoAgentId: string;
  onClose: () => void;
}

const ANALYZE_AND_BUILD = `请先分析当前项目代码库（使用 read_file / list_files / grep），了解技术栈和模块结构。然后根据分析结果，设计适合这个项目的组织架构——确定需要哪些角色、采用什么组织范式、层级怎么划分。设计完成后，将架构写入 charter，并指示 HR 按架构招人。`;

const BRAINSTORM = `我们刚创建了一个新项目。在搭建团队之前，我想先和你聊聊——这个项目的目标是什么，优先级怎么排，有没有特别需要注意的地方。你也可以问我一些问题来帮我理清思路。`;

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
        className="bg-surface-card border border-surface-border rounded-xl shadow-2xl p-6 w-full max-w-lg mx-4"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-white">
            项目已创建 — 接下来做什么？
          </h2>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-white text-xl leading-none transition-colors"
          >
            ✕
          </button>
        </div>

        {/* Options */}
        <div className="space-y-3 mb-4">
          <button
            onClick={() => handleSend(ANALYZE_AND_BUILD)}
            className="w-full text-left px-4 py-3 rounded-lg border border-surface-border bg-surface-alt hover:bg-surface-hover transition-colors"
          >
            <div className="text-white font-medium">
              让 CEO 分析项目并搭建组织架构
            </div>
            <div className="text-gray-400 text-sm mt-0.5">
              CEO 会先阅读代码库，了解技术栈和模块结构，然后设计适合的组织架构
            </div>
          </button>

          <button
            
            onClick={() => handleSend(BRAINSTORM)}
            className="w-full text-left px-4 py-3 rounded-lg border border-surface-border bg-surface-alt hover:bg-surface-hover transition-colors"
          >
            <div className="text-white font-medium">
              与 CEO 讨论项目方向
            </div>
            <div className="text-gray-400 text-sm mt-0.5">
              在搭建团队之前，先和 CEO 聊聊项目目标、优先级和注意事项
            </div>
          </button>
        </div>

        {/* Divider */}
        <div className="flex items-center gap-3 mb-4">
          <div className="flex-1 h-px bg-surface-border" />
          <span className="text-gray-500 text-sm">或者</span>
          <div className="flex-1 h-px bg-surface-border" />
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
            className="flex-1 px-3 py-2 bg-surface-alt border border-surface-border rounded-lg text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 text-sm"
            
          />
          <button
            disabled={!customInput.trim()}
            onClick={() => customInput.trim() && handleSend(customInput.trim())}
            className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-40 text-white rounded-lg text-sm font-medium transition-colors"
          >
            发送
          </button>
        </div>
      </div>
    </div>
  );
}
