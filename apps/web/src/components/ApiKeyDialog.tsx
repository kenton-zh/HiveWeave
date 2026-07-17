import { useState, useEffect } from "react";
import { setApiKey } from "../api";

export default function ApiKeyDialog({ onClose }: { onClose: () => void }) {
  const [key, setKey] = useState(() => {
    try { return localStorage.getItem("hiveweave_api_key") || ""; } catch { return ""; }
  });

  // 入场动效（纯视觉）：遮罩淡入 + 面板滑入
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  const handleSave = () => {
    setApiKey(key.trim() || null);
    onClose();
  };

  const handleClear = () => {
    setApiKey(null);
    setKey("");
  };

  return (
    <div
      className={`fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[2px] transition-opacity duration-200 ${entered ? "opacity-100" : "opacity-0"}`}
      onClick={onClose}
    >
      <div
        className={`bg-g-bg border border-g-border rounded-gmLg shadow-gm-lg p-6 w-96 max-w-[90vw] transform transition-all duration-200 ease-out ${entered ? "opacity-100 translate-y-0 scale-100" : "opacity-0 translate-y-3 scale-[0.98]"}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2.5 mb-4">
          <span className="w-7 h-7 rounded-gm bg-g-blue-bg flex items-center justify-center text-sm shrink-0">🔑</span>
          <h2 className="text-sm font-semibold text-g-fg">API Key 设置</h2>
        </div>
        <p className="text-xs text-g-fg-3 mb-3 leading-relaxed">后端启用 HIVEWEAVE_API_KEY 时需要配置。留空则不携带。</p>
        <input
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") handleSave(); }}
          placeholder="输入 API Key..."
          className="w-full px-3 py-2 text-xs border border-g-border rounded-gm bg-g-bg-soft text-g-fg placeholder-g-fg-4/70 focus:outline-none focus:border-g-blue focus:ring-2 focus:ring-g-blue/15 mb-4 transition-shadow"
          autoFocus
        />
        <div className="flex justify-end gap-2">
          <button onClick={handleClear} className="px-3 py-1.5 text-xs text-g-fg-3 hover:text-red-600 rounded-gm hover:bg-red-50 active:scale-[0.97] transition-all">
            清除
          </button>
          <button onClick={onClose} className="px-3 py-1.5 text-xs text-g-fg-3 hover:text-g-fg rounded-gm hover:bg-g-bg-muted active:scale-[0.97] transition-all">
            取消
          </button>
          <button onClick={handleSave} className="px-3 py-1.5 text-xs bg-g-blue text-white rounded-gm shadow-gm-sm hover:bg-blue-600 active:scale-[0.97] transition-all">
            保存
          </button>
        </div>
      </div>
    </div>
  );
}
