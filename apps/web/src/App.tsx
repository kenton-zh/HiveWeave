import OrgTree from "./components/OrgTree";
import ChatPanel from "./components/ChatPanel";
import WorkLogPanel from "./components/WorkLogPanel";
import { useAppStore } from "./store";

function App() {
  const selectedAgentId = useAppStore((s) => s.selectedAgentId);

  return (
    <div className="h-screen flex flex-col bg-surface">
      {/* Top Bar */}
      <header className="h-14 border-b border-surface-border flex items-center px-6 bg-surface-card shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-accent flex items-center justify-center">
            <span className="text-white font-bold text-sm">H</span>
          </div>
          <h1 className="text-lg font-semibold text-gray-100 tracking-tight">
            HiveWeave
          </h1>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <div className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
            <span className="text-xs text-gray-400">System Online</span>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left Panel - Org Tree (40%) */}
        <div className="w-2/5 border-r border-surface-border flex flex-col">
          <div className="px-4 py-3 border-b border-surface-border bg-surface-card">
            <h2 className="text-sm font-medium text-gray-300">
              Organization Tree
            </h2>
          </div>
          <div className="flex-1 overflow-hidden">
            <OrgTree />
          </div>
        </div>

        {/* Right Panel - Chat (60%) */}
        <div className="w-3/5 flex flex-col">
          <div className="flex-1 overflow-hidden">
            <ChatPanel agentId={selectedAgentId} />
          </div>
          <WorkLogPanel agentId={selectedAgentId} />
        </div>
      </div>
    </div>
  );
}

export default App;
