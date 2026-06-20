import { useState, useRef, useEffect, useCallback } from "react";
import { useAppStore } from "../store";
import { streamChat, getAgent } from "../api";

interface AgentInfo {
  id: string;
  name: string;
  role: string;
  status: string;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: number;
}

const roleLabels: Record<string, string> = {
  architect: "Architect",
  manager: "Manager",
  module_dev: "Developer",
  qa: "QA",
  devops: "DevOps",
};

const statusLabels: Record<string, { text: string; color: string }> = {
  created: { text: "Created", color: "text-gray-400" },
  active: { text: "Active", color: "text-emerald-400" },
  promoted: { text: "Promoted", color: "text-blue-400" },
  receiving: { text: "Receiving", color: "text-amber-400" },
  merging: { text: "Merging", color: "text-purple-400" },
  dissolving: { text: "Dissolving", color: "text-red-400" },
  archived: { text: "Archived", color: "text-gray-500" },
  // Legacy statuses (demo/fallback)
  idle: { text: "Idle", color: "text-gray-400" },
  working: { text: "Working", color: "text-emerald-400" },
  error: { text: "Error", color: "text-red-400" },
  waiting: { text: "Waiting", color: "text-amber-400" },
};

function ChatPanel({ agentId }: { agentId: string | null }) {
  const [agentInfo, setAgentInfo] = useState<AgentInfo | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const chatSessions = useAppStore((s) => s.chatSessions);
  const addMessage = useAppStore((s) => s.addMessage);

  // Fetch agent info when agentId changes
  useEffect(() => {
    if (!agentId) {
      setAgentInfo(null);
      setMessages([]);
      return;
    }

    // Load messages from store for this agent
    setMessages(chatSessions[agentId] || []);

    // Fetch agent details
    async function fetchAgent() {
      try {
        const data = await getAgent(agentId!);
        if (data && typeof data === "object" && data.id) {
          setAgentInfo(data);
        } else {
          setAgentInfo({ id: agentId!, name: "Agent", role: "module_dev", status: "idle" });
        }
      } catch (err) {
        console.error("Failed to fetch agent:", err);
        setAgentInfo({ id: agentId!, name: "Agent", role: "module_dev", status: "idle" });
      }
    }
    fetchAgent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSend = useCallback(() => {
    if (!agentId || !input.trim() || isStreaming) return;

    const userMessage: ChatMessage = {
      id: `${Date.now()}-user`,
      role: "user",
      content: input.trim(),
      timestamp: Date.now(),
    };

    const assistantMessage: ChatMessage = {
      id: `${Date.now()}-assistant`,
      role: "assistant",
      content: "",
      timestamp: Date.now(),
    };

    // Add user message
    addMessage(agentId, userMessage);
    setMessages((prev) => [...prev, userMessage]);
    setInput("");
    setIsStreaming(true);

    // Add placeholder assistant message
    setMessages((prev) => [...prev, assistantMessage]);

    // Stream response
    let accumulated = "";
    abortControllerRef.current = streamChat(
      agentId,
      input.trim(),
      (event) => {
        if (event.type === "text") {
          accumulated += event.data;
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMessage.id
                ? { ...m, content: accumulated }
                : m
            )
          );
        } else if (event.type === "done") {
          setIsStreaming(false);
          // Persist final message
          addMessage(agentId, {
            ...assistantMessage,
            content: accumulated,
          });
        } else if (event.type === "error") {
          setIsStreaming(false);
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantMessage.id
                ? { ...m, content: `Error: ${event.data}` }
                : m
            )
          );
        }
      }
    );
  }, [agentId, input, isStreaming, addMessage]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // No agent selected placeholder
  if (!agentId) {
    return (
      <div className="h-full flex items-center justify-center bg-surface">
        <div className="text-center">
          <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-surface-card border border-surface-border flex items-center justify-center">
            <svg className="w-8 h-8 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
            </svg>
          </div>
          <p className="text-gray-500 text-sm">选择一个 Agent 开始对话</p>
        </div>
      </div>
    );
  }

  const statusInfo = statusLabels[agentInfo?.status || "idle"] || { text: agentInfo?.status || "Unknown", color: "text-gray-400" };

  return (
    <div className="h-full flex flex-col bg-surface">
      {/* Header */}
      {agentInfo && (
        <div className="px-6 py-4 border-b border-surface-border bg-surface-card shrink-0">
          <div className="flex items-center gap-3">
            <div className="flex-1">
              <h3 className="text-base font-semibold text-gray-100">
                {agentInfo.name}
              </h3>
              <div className="flex items-center gap-2 mt-1">
                <span className="text-xs px-2 py-0.5 rounded-full bg-accent/20 text-accent">
                  {roleLabels[agentInfo.role] || agentInfo.role}
                </span>
                <span className={`text-xs ${statusInfo.color}`}>
                  {statusInfo.text}
                </span>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
          >
            <div
              className={`
                max-w-[80%] rounded-2xl px-4 py-3
                ${msg.role === "user"
                  ? "bg-accent text-white"
                  : "bg-surface-card border border-surface-border text-gray-200"
                }
              `}
            >
              <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
              {msg.role === "assistant" && isStreaming && msg.content === "" && (
                <div className="flex gap-1">
                  <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                  <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                  <span className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
                </div>
              )}
            </div>
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="px-6 py-4 border-t border-surface-border bg-surface-card shrink-0">
        <div className="flex gap-3">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="输入消息... (Enter 发送, Shift+Enter 换行)"
            className="flex-1 bg-surface border border-surface-border rounded-xl px-4 py-3 text-sm text-gray-100 placeholder-gray-500 resize-none focus:outline-none focus:border-accent"
            rows={1}
            disabled={isStreaming}
          />
          <button
            onClick={handleSend}
            disabled={!input.trim() || isStreaming}
            className="px-6 py-3 bg-accent hover:bg-accent-dim disabled:bg-surface-border disabled:text-gray-500 text-white rounded-xl text-sm font-medium transition-colors disabled:cursor-not-allowed"
          >
            {isStreaming ? "..." : "发送"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default ChatPanel;
