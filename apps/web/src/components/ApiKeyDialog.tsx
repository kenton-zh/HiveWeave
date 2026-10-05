import { useId, useState } from "react";
import { setApiKey } from "../api";
import Modal from "./Modal";

export default function ApiKeyDialog({ onClose }: { onClose: () => void }) {
  const [key, setKey] = useState(() => {
    try { return localStorage.getItem("hiveweave_api_key") || ""; } catch { return ""; }
  });
  const titleId = useId();

  const handleSave = () => {
    setApiKey(key.trim() || null);
    onClose();
  };

  const handleClear = () => {
    setApiKey(null);
    setKey("");
  };

  return (
    <Modal onClose={onClose} labelledBy={titleId} className="p-6 w-96 max-w-[90vw]">
      <div className="flex items-center gap-2.5 mb-4">
        <span className="w-7 h-7 rounded-gm bg-g-blue-bg flex items-center justify-center shrink-0" aria-hidden="true">
          <svg className="w-4 h-4 text-g-blue" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 7a4 4 0 11-8 0 4 4 0 018 0zM12 15v6m-3-3h6" />
          </svg>
        </span>
        <h2 id={titleId} className="text-sm font-semibold text-g-fg">API Key 设置</h2>
      </div>
      <p className="text-xs text-g-fg-3 mb-3 leading-relaxed" id={`${titleId}-desc`}>后端启用 HIVEWEAVE_API_KEY 时需要配置。留空则不携带。</p>
      <input
        type="password"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") handleSave(); }}
        placeholder="输入 API Key..."
        aria-label="API Key"
        aria-describedby={`${titleId}-desc`}
        className="w-full px-3 py-2 text-xs border border-g-border rounded-gm bg-g-bg-soft text-g-fg placeholder-g-fg-4 focus:outline-none focus:border-g-blue focus:ring-2 focus:ring-g-blue/15 mb-4 transition-shadow"
      />
      <div className="flex justify-end gap-2">
        <button onClick={handleClear} className="px-3 py-1.5 text-xs text-g-fg-3 hover:text-g-red rounded-gm hover:bg-g-red-bg active:scale-[0.97] transition-all">
          清除
        </button>
        <button onClick={onClose} className="px-3 py-1.5 text-xs text-g-fg-3 hover:text-g-fg rounded-gm hover:bg-g-bg-muted active:scale-[0.97] transition-all">
          取消
        </button>
        <button onClick={handleSave} className="px-3 py-1.5 text-xs bg-g-blue text-white rounded-gm shadow-gm-sm hover:bg-g-blue active:scale-[0.97] transition-all">
          保存
        </button>
      </div>
    </Modal>
  );
}
