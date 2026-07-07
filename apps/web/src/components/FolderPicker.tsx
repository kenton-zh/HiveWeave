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
        <div className="bg-surface border border-surface-border rounded-lg px-6 py-4 flex items-center gap-3">
          <div className="animate-spin w-4 h-4 border-2 border-accent border-t-transparent rounded-full" />
          <span className="text-sm text-gray-400">等待选择文件夹...</span>
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
  const [selectedEntry, setSelectedEntry] = useState<string | null>(null);
  const addressRef = useRef<HTMLInputElement>(null);

  const navigate = async (dirPath?: string) => {
    setLoading(true);
    try {
      const result = await browseDirectory(dirPath);
      setData(result);
      setAddressBar(result.currentPath || "");
    } catch {
      // keep previous state on error
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onCancel}>
      <div
        className="bg-surface border border-surface-border rounded-lg shadow-2xl w-[640px] max-h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-surface-border">
          <svg className="w-5 h-5 text-accent shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
              d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
          </svg>
          <span className="text-sm text-gray-300 font-medium shrink-0">选择工作区目录</span>
        </div>

        {/* Address bar + navigation */}
        <div className="flex items-center gap-2 px-4 py-2 border-b border-surface-border">
          <button
            disabled={!data?.parentPath || loading}
            onClick={() => data?.parentPath && navigate(data.parentPath)}
            className="p-1 rounded hover:bg-surface-hover disabled:opacity-30 disabled:cursor-not-allowed text-gray-400 hover:text-gray-200"
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
              className="flex-1 px-2 py-1 text-xs bg-surface-deep border border-accent rounded text-gray-200 focus:outline-none font-mono"
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
              className="flex-1 px-2 py-1 text-xs bg-surface-deep border border-surface-border rounded text-gray-400 cursor-text font-mono truncate hover:border-gray-600"
              title="点击编辑路径，或直接粘贴完整路径"
            >
              {addressBar || "..."}
            </div>
          )}
        </div>

        {/* Drive shortcuts (Windows) */}
        {data && data.drives && data.drives.length > 0 && (
          <div className="flex items-center gap-1 px-4 py-1.5 border-b border-surface-border overflow-x-auto">
            {data.drives.map((drive) => (
              <button
                key={drive}
                onClick={() => navigate(drive)}
                disabled={loading}
                className={`px-2 py-0.5 text-xs rounded border shrink-0 transition-colors ${
                  data.currentPath?.startsWith(drive)
                    ? "border-accent/50 text-accent bg-accent/10"
                    : "border-surface-border text-gray-500 hover:text-gray-300 hover:border-gray-600"
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
            <div className="flex items-center justify-center h-full text-gray-500 text-sm">加载中...</div>
          ) : data && data.entries.length === 0 ? (
            <div className="flex items-center justify-center h-full text-gray-500 text-sm">（空目录）</div>
          ) : data ? (
            <div
              className="grid grid-cols-2 gap-0.5 focus:outline-none"
              tabIndex={0}
              onKeyDown={(e) => {
                // BUG-031 fix: Enter navigates into selected directory
                if (e.key === "Enter" && selectedEntry) {
                  navigate(selectedEntry);
                }
              }}
            >
              {data.entries.map((entry) => (
                <button
                  key={entry.fullPath}
                  onClick={() => setSelectedEntry(entry.fullPath ?? null)}
                  onDoubleClick={() => entry.fullPath && navigate(entry.fullPath)}
                  className={`flex items-center gap-2 px-3 py-2 rounded text-left group transition-colors ${
                    selectedEntry === entry.fullPath
                      ? "bg-accent/20 border border-accent/40"
                      : "hover:bg-surface-hover border border-transparent"
                  }`}
                >
                  <svg className="w-5 h-5 text-yellow-500/80 shrink-0" fill="currentColor" viewBox="0 0 24 24">
                    <path d="M10 4H4a2 2 0 00-2 2v12a2 2 0 002 2h16a2 2 0 002-2V8a2 2 0 00-2-2h-8l-2-2z" />
                  </svg>
                  <span className="text-sm text-gray-300 group-hover:text-gray-100 truncate">{entry.name}</span>
                </button>
              ))}
            </div>
          ) : null}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-4 py-3 border-t border-surface-border">
          <div className="text-xs text-gray-500 truncate max-w-[60%]" title={data?.currentPath}>
            {data?.currentPath || "..."}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onCancel}
              className="px-4 py-1.5 text-sm text-gray-400 hover:text-gray-200 border border-surface-border rounded hover:border-gray-600 transition-colors"
            >
              取消
            </button>
            <button
              onClick={() => data?.currentPath && onSelect(data.currentPath)}
              disabled={!data || loading}
              className="px-4 py-1.5 text-sm bg-accent text-white rounded hover:bg-accent/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              选择文件夹
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
