import { useState, useEffect, useRef } from "react";
import { browseDirectory, type BrowseResult } from "../api";

interface FolderPickerProps {
  initialPath?: string;
  onSelect: (path: string) => void;
  onCancel: () => void;
}

/**
 * Folder picker with dual mode:
 * - Electron: delegates to native OS folder picker via IPC
 * - Web browser: renders a custom directory browser modal
 */
export default function FolderPicker({ initialPath, onSelect, onCancel }: FolderPickerProps) {
  const isElectron = typeof window !== "undefined" && window.electronAPI?.isElectron;
  const dialogOpened = useRef(false);

  // ── Electron mode: open native dialog immediately ──────────
  // StrictMode double-invocation guard:
  //   We delay the IPC call by 100ms. StrictMode unmounts ~microseconds after
  //   mount, so cleanup clears mount-1's timer before it fires. Mount-2's timer
  //   then executes normally. Without the delay, both mounts would open a dialog.
  useEffect(() => {
    if (!isElectron) return;
    if (dialogOpened.current) return;
    dialogOpened.current = true;
    let cancelled = false;
    const timer = setTimeout(() => {
      window.electronAPI!.selectFolder().then((folderPath) => {
        if (cancelled) return;
        if (folderPath) {
          onSelect(folderPath);
        } else {
          onCancel();
        }
      });
    }, 100);
    return () => { cancelled = true; clearTimeout(timer); dialogOpened.current = false; };
  }, [isElectron]);

  // In Electron mode, show a brief loading state while the native dialog is open
  if (isElectron) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
        <div className="bg-g-bg border border-g-border rounded-lg px-6 py-4 flex items-center gap-3">
          <div className="animate-spin w-4 h-4 border-2 border-g-blue border-t-transparent rounded-full" />
          <span className="text-sm text-g-fg-3">等待选择文件夹...</span>
        </div>
      </div>
    );
  }

  // ── Web mode: custom directory browser ──────────────────────
  return <WebFolderPicker initialPath={initialPath} onSelect={onSelect} onCancel={onCancel} />;
}

// ────────────────────────────────────────────────────────────────
// Web fallback: custom directory browser using /api/fs/browse
// ────────────────────────────────────────────────────────────────

function WebFolderPicker({
  initialPath,
  onSelect,
  onCancel,
}: FolderPickerProps) {
  const [data, setData] = useState<BrowseResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [addressBar, setAddressBar] = useState("");
  const [addressEditing, setAddressEditing] = useState(false);
  const addressRef = useRef<HTMLInputElement>(null);

  // 入场动效（纯视觉）：遮罩淡入 + 面板滑入
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  const navigate = async (dirPath?: string) => {
    setLoading(true);
    try {
      const result = await browseDirectory(dirPath);
      setData(result);
      setAddressBar(result.currentPath || "");
    } catch {
      // On error, clear stale data so we don't show deleted directories
      setData(null);
    }
    setLoading(false);
  };

  useEffect(() => {
    navigate(initialPath);
  }, []);

  useEffect(() => {
    if (addressEditing && addressRef.current) {
      addressRef.current.focus();
      addressRef.current.select();
    }
  }, [addressEditing]);

  const handleAddressSubmit = () => {
    const trimmed = addressBar.trim();
    if (trimmed) {
      navigate(trimmed);
    }
    setAddressEditing(false);
  };

  return (
    <div
      className={`fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-[2px] transition-opacity duration-200 ${entered ? "opacity-100" : "opacity-0"}`}
      onClick={onCancel}
    >
      <div
        className={`bg-g-bg border border-g-border rounded-gmLg shadow-gm-lg w-[640px] max-h-[80vh] flex flex-col transform transition-all duration-200 ease-out ${entered ? "opacity-100 translate-y-0 scale-100" : "opacity-0 translate-y-3 scale-[0.98]"}`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-g-border">
          <svg className="w-5 h-5 text-g-blue shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
              d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
          </svg>
          <span className="text-sm text-g-fg font-medium shrink-0">选择工作区目录</span>
        </div>

        {/* Address bar + navigation */}
        <div className="flex items-center gap-2 px-4 py-2 border-b border-g-border">
          <button
            disabled={!data?.parentPath || loading}
            onClick={() => data?.parentPath && navigate(data.parentPath)}
            className="p-1 rounded hover:bg-g-bg-soft disabled:opacity-30 disabled:cursor-not-allowed text-g-fg-3 hover:text-g-fg"
            title="上级目录"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 15l7-7 7 7" />
            </svg>
          </button>

          {addressEditing ? (
            <input
              ref={addressRef}
              value={addressBar}
              onChange={(e) => setAddressBar(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleAddressSubmit();
                if (e.key === "Escape") { setAddressEditing(false); if (data) setAddressBar(data.currentPath ?? ""); }
              }}
              onBlur={handleAddressSubmit}
              className="flex-1 px-2 py-1 text-xs bg-g-bg-muted border border-g-blue rounded text-g-fg focus:outline-none font-mono"
            />
          ) : (
            <div
              onClick={() => setAddressEditing(true)}
              onPaste={(e) => {
                // BUG-031 fix: support pasting a path directly into the
                // address bar without clicking to enter edit mode first.
                e.preventDefault();
                const pasted = e.clipboardData?.getData("text")?.trim();
                if (pasted) {
                  setAddressBar(pasted);
                  // Navigate immediately — user clearly wants to go there
                  navigate(pasted);
                }
              }}
              className="flex-1 px-2 py-1 text-xs bg-g-bg-muted border border-g-border rounded text-g-fg-3 cursor-text font-mono truncate hover:border-g-border"
              title="点击编辑路径，或直接粘贴完整路径"
            >
              {addressBar || "..."}
            </div>
          )}
        </div>

        {/* Drive shortcuts (Windows) */}
        {data && data.drives && data.drives.length > 0 && (
          <div className="flex items-center gap-1 px-4 py-1.5 border-b border-g-border overflow-x-auto">
            {data.drives.map((drive) => (
              <button
                key={drive}
                onClick={() => navigate(drive)}
                disabled={loading}
                className={`px-2.5 py-0.5 text-xs rounded-full border shrink-0 active:scale-[0.96] transition-all ${
                  data.currentPath?.startsWith(drive)
                    ? "border-g-blue/50 text-g-blue bg-g-blue-bg/60 shadow-gm-sm"
                    : "border-g-border text-g-fg-4 hover:text-g-fg hover:border-g-border-strong hover:bg-g-bg-soft"
                }`}
              >
                {drive.replace("\\", "")}
              </button>
            ))}
          </div>
        )}

        {/* Directory listing */}
        <div className="flex-1 overflow-y-auto px-2 py-2 min-h-[300px] max-h-[50vh]">
          {loading && !data ? (
            <div className="flex items-center justify-center h-full text-g-fg-4 text-sm">加载中...</div>
          ) : data && data.entries.length === 0 ? (
            <div className="flex items-center justify-center h-full text-g-fg-4 text-sm">（空目录）</div>
          ) : data ? (
            <div
              className="grid grid-cols-2 gap-0.5"
            >
              {data.entries.map((entry) => (
                <button
                  key={entry.fullPath}
                  onClick={() => entry.fullPath && navigate(entry.fullPath)}
                  className="flex items-center gap-2 px-3 py-2 rounded-gm text-left group transition-colors hover:bg-g-blue-bg/50 border border-transparent hover:border-g-blue/20"
                >
                  <svg className="w-5 h-5 text-yellow-500/80 shrink-0 group-hover:scale-110 transition-transform" fill="currentColor" viewBox="0 0 24 24">
                    <path d="M10 4H4a2 2 0 00-2 2v12a2 2 0 002 2h16a2 2 0 002-2V8a2 2 0 00-2-2h-8l-2-2z" />
                  </svg>
                  <span className="text-sm text-g-fg group-hover:text-g-fg truncate">{entry.name}</span>
                </button>
              ))}
            </div>
          ) : null}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-4 py-3 border-t border-g-border">
          <div className="text-xs text-g-fg-4 truncate max-w-[60%]" title={data?.currentPath}>
            {data?.currentPath || "..."}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onCancel}
              className="px-4 py-1.5 text-sm text-g-fg-3 hover:text-g-fg border border-g-border rounded-gm hover:bg-g-bg-muted hover:border-g-border-strong active:scale-[0.97] transition-all"
            >
              取消
            </button>
            <button
              onClick={() => data?.currentPath && onSelect(data.currentPath)}
              disabled={!data || loading}
              className="px-4 py-1.5 text-sm bg-g-blue text-white rounded-gm shadow-gm-sm hover:bg-blue-600 active:scale-[0.97] disabled:opacity-50 disabled:cursor-not-allowed transition-all"
            >
              选择文件夹
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
