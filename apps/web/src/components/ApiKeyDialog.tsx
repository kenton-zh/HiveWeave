import { useState } from "react";
import { setApiKey } from "../api";

export default function ApiKeyDialog({ onClose }: { onClose: () => void }) {
  const [key, setKey] = useState(() => {
    try { return localStorage.getItem("hiveweave_api_key") || ""; } catch { return ""; }
  });

  const handleSave = () => {
    setApiKey(key.trim() || null);
    onClose();
  };

  const handleClear = () => {
    setApiKey(null);
    setKey("");
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div className="bg-white rounded-gm shadow-lg p-6 w-96 max-w-[90vw]" onClick={(e) => e.stopPropagation()}>
        <h2 className="text-sm font-medium text-g-fg mb-4">API Key 设置</h2>
        <p className="text-xs text-g-fg-3 mb-3">后端启用 HIVEWEAVE_API_KEY 时需要配置。留空则不携带。</p>
        <input
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") handleSave(); }}
          placeholder="输入 API Key..."
          className="w-full px-3 py-2 text-xs border border-g-line rounded-gm bg-white text-g-fg focus:outline-none focus:border-g-blue mb-4"
          autoFocus
        />
        <div className="flex justify-end gap-2">
          <button onClick={handleClear} className="px-3 py-1.5 text-xs text-g-fg-3 hover:text-g-fg transition-colors">
            清除
          </button>
          <button onClick={onClose} className="px-3 py-1.5 text-xs text-g-fg-3 hover:text-g-fg transition-colors">
            取消
          </button>
          <button onClick={handleSave} className="px-3 py-1.5 text-xs bg-g-blue text-white rounded-gm hover:opacity-90 transition-opacity">
            保存
          </button>
        </div>
      </div>
    </div>
  );
}
