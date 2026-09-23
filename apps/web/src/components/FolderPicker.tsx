import { useState, useEffect, useRef } from "react";
import { browseDirectory, pickFolderNative, type BrowseResult } from "../api";

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
        <div className="bg-g-bg border border-g-border rounded-gm px-6 py-4 flex items-center gap-3">
          <div className="animate-spin w-4 h-4 border-2 border-g-blue border-t-transparent rounded-full" />
          <span className="text-sm text-g-fg-3">等待选择文件夹...</span>
        </div>
      </div>
    );
  }

  // ── Web mode: 先试系统原生对话框，弹不出来才回退内置浏览器 ──
  return <NativeThenWebPicker initialPath={initialPath} onSelect={onSelect} onCancel={onCancel} />;
}

/**
 * 把浏览失败翻译成用户能据以行动的一句话（2026-09-23 审计 P2-1）。
 * 原先只有「无法读取该目录」+ 原始 message，用户分不清是自己路径输错了、
 * 后端没起来、还是没权限 —— 而这恰恰是「选不了路径」报告里最缺的信息。
 */
function describeBrowseError(msg: string): string {
  if (/HTTP 5\d\d/.test(msg) || /Failed to fetch|NetworkError|load failed/i.test(msg)) {
    return "后端未就绪，请稍候重试";
  }
  if (/HTTP 404/.test(msg)) return "该路径不存在";
  if (/HTTP 403/.test(msg)) return "该目录无权限访问";
  if (/HTTP 401/.test(msg)) return "需要 API Key";
  return "无法读取该目录";
}

/**
 * 等待系统对话框期间的轻量遮罩（与 Electron 分支同一观感）。
 *
 * ⚠ 必须给逃生口（2026-09-23 审计 P1-1）：系统对话框一旦因为别的原因迟迟不
 * 返回（远程会话、被杀、卡住），这片 `fixed inset-0` 遮罩会把整个网页端锁死 ——
 * 它没有按钮、不吃 Escape、点背景也没反应，用户连「新建项目」都退不出来。
 */
function PickerWaitingOverlay({
  text,
  onCancel,
  onSwitchToWeb,
}: {
  text: string;
  onCancel: () => void;
  onSwitchToWeb: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-[2px]"
      onClick={onCancel}
    >
      <div
        className="bg-g-bg border border-g-border rounded-gmLg shadow-gm-lg px-6 py-4 flex flex-col items-center gap-3"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3">
          <div className="animate-spin w-4 h-4 border-2 border-g-blue border-t-transparent rounded-full" />
          <span className="text-sm text-g-fg-3">{text}</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={onSwitchToWeb}
            className="px-3 py-1 text-xs text-g-blue border border-g-blue/40 rounded-gm hover:bg-g-blue-bg/60 active:scale-[0.97] transition-all"
          >
            改用网页版浏览
          </button>
          <button
            onClick={onCancel}
            className="px-3 py-1 text-xs text-g-fg-3 border border-g-border rounded-gm hover:bg-g-bg-muted hover:text-g-fg active:scale-[0.97] transition-all"
          >
            取消
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * Web 形态的两级策略（2026-09-23）：
 *   ① **优先**让后端弹**系统原生**文件夹选择器 —— 纯浏览器里这是唯一能同时拿到
 *      「系统原生外观 + 绝对路径」的途径（浏览器自身拿不到绝对路径，详见
 *      `api/rest.ts` 中 `pickFolderNative` 的说明，以及 OpenCode web 模式
 *      anomalyco/opencode#7597 的同款缺陷）；
 *   ② 只有后端明确报弹不出来（远程访问 / 无桌面会话的 EXE / tkinter 缺失）
 *      才回退到内置的网页版目录浏览器。
 */
function NativeThenWebPicker(props: FolderPickerProps) {
  // `?folderPath=` 是给自动化/深链直达用的（见 docs/qa/e2e-fullrun15）：它要的是
  // 「URL 阶段就落到目标目录」，而真弹一个模态系统对话框只会把 E2E 挂死（等人去
  // 点）。所以带该参数时直接走网页版浏览器，不探测原生（审计 2026-09-23 P1-2）。
  const [mode, setMode] = useState<"probing" | "web" | "busy">(
    props.initialPath ? "web" : "probing",
  );
  const [busyReason, setBusyReason] = useState("");
  // 只发一次请求。StrictMode 会把 effect 跑两遍 —— 不加这道闩会弹出**两个**
  // 系统对话框。
  const probed = useRef(false);
  // 是否仍挂载。真卸载后到达的结果必须丢掉：用户可能已经关掉弹层，之后才在
  // 那个还开着的系统对话框里选目录 —— 绝不能拿这个结果去建项目。
  //
  // ⚠ 必须与 wanted 分开。若沿用 Electron 分支那种「同一个 effect 里放 cancelled
  //   标志 + cleanup 置位」的写法，StrictMode 的假卸载会把**唯一那次**请求的
  //   结果丢掉，而重挂时 probed 又拦住重发 —— 界面就永远停在「等待系统文件夹
  //   选择器...」（2026-09-23 实测踩过，四种分支全部卡死）。
  const alive = useRef(true);
  // 是否仍**想要**这次原生结果。用户点「改用网页版浏览」或落到 busy 之后，那次
  // 仍在途的请求可能随时带着 path 回来 —— 不看清这个标志就会在他正浏览网页版、
  // 从未确认的情况下直接把项目建了（审计 2026-09-23 P1-1 附带项）。
  const wanted = useRef(true);

  useEffect(() => {
    if (props.initialPath) return; // 深链直达：不探测原生
    if (probed.current) return;
    probed.current = true;
    pickFolderNative()
      .then((r) => {
        if (!alive.current || !wanted.current) return;
        if (r.busy) {
          wanted.current = false;
          setBusyReason(r.reason || "已有一个目录选择窗口在等待，请先完成它");
          setMode("busy");
          return;
        }
        if (r.available) {
          // 用户取消（path=null）→ 直接收摊，不要再弹网页版造成二次困惑
          if (r.path) props.onSelect(r.path);
          else props.onCancel();
          return;
        }
        wanted.current = false;
        setMode("web");
      })
      .catch(() => {
        if (alive.current && wanted.current) {
          wanted.current = false;
          setMode("web");
        }
      });
  }, []);

  useEffect(() => {
    alive.current = true; // 假卸载后重挂要复位
    return () => {
      alive.current = false;
    };
  }, []);

  /** 放弃原生（在途结果随即作废），改用网页版浏览 */
  const switchToWeb = () => {
    wanted.current = false;
    setMode("web");
  };

  if (mode === "probing") {
    return (
      <PickerWaitingOverlay
        text="等待系统文件夹选择器..."
        onCancel={props.onCancel}
        onSwitchToWeb={switchToWeb}
      />
    );
  }
  if (mode === "busy") {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
        <div className="bg-g-bg border border-g-border rounded-gmLg shadow-gm-lg w-[360px] px-5 py-4 flex flex-col gap-3">
          <span className="text-sm text-g-fg font-medium">已有选择窗口在等待</span>
          <span className="text-xs text-g-fg-3">{busyReason}</span>
          <button
            onClick={props.onCancel}
            className="self-end px-3 py-1 text-xs bg-g-blue text-white rounded-gm hover:bg-g-blue active:scale-[0.97] transition-all"
          >
            知道了
          </button>
        </div>
      </div>
    );
  }
  return <WebFolderPicker {...props} />;
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

  // ── 失败可见化（2026-09-23）────────────────────────────────
  // 病灶：原先 `catch { setData(null) }` 把一切失败吞成「data === null」，
  //   而 `data === null` 时盘符行不渲染（它读 data.drives）、列表区渲染 null
  //   ⇒ 弹窗只剩「地址栏 ... / 空白 / 底部 ...」。
  //   用户看到的就是「选不了路径」，且没有任何线索说明为什么、也没法重试。
  //   （实测：后端 200 + 219 项时一切正常，所以空白的唯一成因就是那次请求失败。）
  // 三处修复：① 错误态独立存储并渲染原因 + 重试；② 盘符与 data 解耦，
  //   失败时仍能靠盘符逃生；③ 首次加载失败自动重试一次（覆盖后端刚启动/
  //   瞬时断连这类一次性故障）。
  const [error, setError] = useState<string | null>(null);
  const [drives, setDrives] = useState<string[]>([]);
  /** 本次「要去的目标路径」（undefined = 后端默认，即用户主目录），供重试复用 */
  const targetRef = useRef<string | undefined>(initialPath);
  /**
   * 导航请求序号（2026-09-23 独立审计 P1-A）：只允许「最新一路」写状态。
   * 没有它时慢的旧响应会覆盖刚点出来的新目录（实测：先点慢目录再点快目录，
   * 最终停在慢的那个）；更糟的是 autoRetry 把失败分支推迟 400ms，可能让
   * **已经渲染成功的列表**被同一组件另一路失败清空成错误卡片。
   */
  const seqRef = useRef(0);

  // 入场动效（纯视觉）：遮罩淡入 + 面板滑入
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  /**
   * 导航到 dirPath（undefined = 后端默认目录）。
   *
   * autoRetry=true 时失败会**再试一次**（间隔 400ms）：新建项目弹窗最常见的
   * 失败模式是「后端还在启动 / 刚重启」，重试一次即可自愈，不必让用户干瞪眼。
   */
  const navigate = async (dirPath?: string, opts?: { autoRetry?: boolean }) => {
    const seq = ++seqRef.current;
    const alive = () => seq === seqRef.current;
    targetRef.current = dirPath;
    setLoading(true);
    setError(null);
    const attempts = opts?.autoRetry ? 2 : 1;
    let lastErr = "";
    for (let i = 0; i < attempts; i++) {
      try {
        const result = await browseDirectory(dirPath);
        if (!alive()) return; // 过期响应：不得覆盖更新的导航
        setData(result);
        setAddressBar(result.currentPath || "");
        // 盘符独立于 data：失败时清空了 data 也要保住逃生路径
        setDrives(result.drives ?? []);
        setError(null);
        setLoading(false);
        return;
      } catch (e) {
        lastErr = e instanceof Error ? e.message : String(e);
        if (i < attempts - 1) await new Promise((r) => setTimeout(r, 400));
      }
    }
    if (!alive()) return; // 过期的失败同样不许清空新数据
    // On error, clear stale data so we don't show deleted directories
    setData(null);
    setError(lastErr || "未知错误");
    setLoading(false);
  };

  useEffect(() => {
    navigate(initialPath, { autoRetry: true });
    // 卸载/重挂时作废在途请求，免得那 400ms 重试定时器落地到已销毁的组件
    return () => {
      seqRef.current++;
    };
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
            className="p-1 rounded-gm hover:bg-g-bg-soft disabled:opacity-30 disabled:cursor-not-allowed text-g-fg-3 hover:text-g-fg"
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
              className="flex-1 px-2 py-1 text-xs bg-g-bg-muted border border-g-blue rounded-gm text-g-fg font-mono"
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
              className="flex-1 px-2 py-1 text-xs bg-g-bg-muted border border-g-border rounded-gm text-g-fg-3 cursor-text font-mono truncate hover:border-g-border"
              title="点击编辑路径，或直接粘贴完整路径"
            >
              {addressBar || "..."}
            </div>
          )}
        </div>

        {/* Drive shortcuts (Windows) — 独立于 data，失败时仍是逃生路径 */}
        {drives.length > 0 && (
          <div className="flex items-center gap-1 px-4 py-1.5 border-b border-g-border overflow-x-auto">
            {drives.map((drive) => (
              <button
                key={drive}
                onClick={() => navigate(drive)}
                disabled={loading}
                className={`px-2.5 py-0.5 text-xs rounded-full border shrink-0 active:scale-[0.96] transition-all ${
                  data?.currentPath?.startsWith(drive)
                    ? "border-g-blue/50 text-g-blue bg-g-blue-bg/60 shadow-gm-sm"
                    : "border-g-border text-g-fg-4 hover:text-g-fg hover:border-g-border-strong hover:bg-g-bg-soft"
                }`}
              >
                {drive.replace("\\", "")}
              </button>
            ))}
          </div>
        )}

        {/* 后端返回 200 但目录读取受限 / 有条目读不到 —— 不谎报「空目录」。
            skipped 必须被消费：本机主目录就有 2 个失效重解析点，原先这种情况
            在 UI 上完全静默（审计 2026-09-23 P2-1：字段零读取 = 空头承诺）。 */}
        {(data?.error || (data?.skipped ?? 0) > 0) && (
          <div className="mx-4 mt-2 px-3 py-1.5 rounded-gm bg-g-yellow-bg text-xs text-g-yellow break-all">
            {data?.error ? `部分内容读取受限：${data.error}` : ""}
            {data?.error && (data?.skipped ?? 0) > 0 ? "；" : ""}
            {(data?.skipped ?? 0) > 0
              ? `另有 ${data?.skipped} 项读不到（已按 0 显示）`
              : ""}
          </div>
        )}

        {/* Directory listing */}
        <div className="flex-1 overflow-y-auto px-2 py-2 min-h-[300px] max-h-[50vh]">
          {error ? (
            <div className="flex flex-col items-center justify-center gap-2 h-full px-6 text-center">
              <svg className="w-7 h-7 text-g-red shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                  d="M12 9v3.75m0 3.75h.008M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
              </svg>
              <div className="text-sm text-g-fg-3">{describeBrowseError(error)}</div>
              {/* 显示是**哪个**路径失败：首屏失败时地址栏还是空的（addressBar 尚未
                  被赋值），用户根本不知道自己卡在哪（审计 2026-09-23 P2-4） */}
              <div className="text-xs text-g-fg-3 break-all max-w-full">
                {targetRef.current || "用户主目录"}
              </div>
              <div className="text-xs text-g-fg-4 break-all max-w-full">{error}</div>
              <div className="flex items-center gap-2 mt-1">
                <button
                  onClick={() => navigate(targetRef.current, { autoRetry: true })}
                  className="px-3 py-1 text-xs text-g-blue border border-g-blue/40 rounded-gm hover:bg-g-blue-bg/60 active:scale-[0.97] transition-all"
                >
                  重试
                </button>
                {/* 首屏就失败时盘符还没拿到（drives 来自响应体），这条是本页面唯一
                    确定的逃生口 —— 没有它用户会被困在错误卡片上，只剩地址栏可赌 */}
                <button
                  onClick={() => navigate(undefined, { autoRetry: true })}
                  className="px-3 py-1 text-xs text-g-fg-3 border border-g-border rounded-gm hover:bg-g-bg-muted hover:text-g-fg active:scale-[0.97] transition-all"
                >
                  回到主目录
                </button>
              </div>
            </div>
          ) : loading && !data ? (
            <div className="flex items-center justify-center h-full text-g-fg-4 text-sm">加载中...</div>
          ) : data && data.entries.length === 0 ? (
            <div className="flex items-center justify-center h-full text-g-fg-4 text-sm">（空目录）</div>
          ) : data ? (
            <div
              className="grid grid-cols-2 gap-0.5"
            >
              {data.entries
                // 只列目录：这是**目录**选择器，而文件条目点下去之后后端会把
                // target 改成它的父目录（filesystem.py 的 is_file 分支）、返回
                // 的还是同一个目录 —— 用户看到的是「点了没反应」。文件对选工作区
                // 毫无用处（审计 2026-09-23 P2-8）。
                .filter((entry) => entry.isDir || entry.is_dir)
                .map((entry) => (
                <button
                  key={entry.fullPath}
                  onClick={() => entry.fullPath && navigate(entry.fullPath)}
                  className="flex items-center gap-2 px-3 py-2 rounded-gm text-left group transition-colors hover:bg-g-blue-bg/50 border border-transparent hover:border-g-blue/20"
                >
                  <svg className="w-5 h-5 text-g-yellow-vivid/80 shrink-0 group-hover:scale-110 transition-transform" fill="currentColor" viewBox="0 0 24 24">
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
              className="px-4 py-1.5 text-sm bg-g-blue text-white rounded-gm shadow-gm-sm hover:bg-g-blue active:scale-[0.97] disabled:opacity-50 disabled:cursor-not-allowed transition-all"
            >
              选择文件夹
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
