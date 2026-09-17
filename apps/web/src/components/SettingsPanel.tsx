import { useState, useEffect } from "react";
import {
  getMcpServers,
  addMcpServer,
  removeMcpServer,
  getProjects,
  getSettings,
  upsertSetting,
} from "../api";
import type { McpServer } from "../api";
import ConfirmDialog from "./ConfirmDialog";

/**
 * 设置面板——两个分区：无人值守模式（按项目开关）+ MCP 服务器管理。
 * 视觉与交互对齐 ApiKeyDialog；删除用 ConfirmDialog（可测试）。
 */
export default function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // ── 无人值守分区状态（unattended_mode:<project_id>，true/1/yes/on 均认）──
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [unattended, setUnattended] = useState<Record<string, boolean>>({});
  const [unattendedSaving, setUnattendedSaving] = useState<Record<string, boolean>>({});
  const [unattendedUnknown, setUnattendedUnknown] = useState(false);
  const [globalUnattended, setGlobalUnattended] = useState(false);

  // 添加表单状态
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<"http" | "stdio">("http");
  const [endpoint, setEndpoint] = useState(""); // http → url；stdio → command
  const [argsText, setArgsText] = useState(""); // stdio 参数，每行一个
  const [envJson, setEnvJson] = useState("{}");
  const [saving, setSaving] = useState(false);

  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  // 入场动效（纯视觉）：遮罩淡入 + 面板滑入
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  const refresh = async () => {
    try {
      // 三个请求独立 catch（审计 L2）：任一失败不连坐其他分区
      const [list] = await Promise.all([getMcpServers().catch(() => [])]);
      setServers(list);
      const projs = await getProjects().catch(() => []);
      setProjects(
        (projs as { id: string; name: string }[]).map((pr) => ({
          id: pr.id,
          name: pr.name,
        })),
      );
      const ON = ["1", "true", "yes", "on"];
      try {
        const settingsData = await getSettings();
        const flags: Record<string, boolean> = {};
        let globalOn = false;
        for (const item of (settingsData as { settings: { key: string; value?: string }[] })
          .settings || []) {
          const on = ON.includes(String(item.value || "").trim().toLowerCase());
          if (item.key === "unattended_mode") {
            globalOn = on; // 审计 M1：全局回退键——项目显式 false 压不住它
          } else if (item.key.startsWith("unattended_mode:")) {
            const pid = item.key.slice("unattended_mode:".length);
            flags[pid] = on;
          }
        }
        setUnattended(flags);
        setGlobalUnattended(globalOn);
        setUnattendedUnknown(false);
      } catch {
        setUnattendedUnknown(true); // 状态未知：禁止把未知项写成开
      }
    } catch (err: any) {
      setError(`加载失败: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  const toggleUnattended = async (pid: string) => {
    setError(""); // 审计 L3：上次失败的横幅不应挂在成功重试之后
    const next = !unattended[pid];
    setUnattendedSaving((m) => ({ ...m, [pid]: true }));
    const prev = unattended[pid];
    setUnattended((m) => ({ ...m, [pid]: next }));
    try {
      await upsertSetting(`unattended_mode:${pid}`, next ? "true" : "false");
    } catch (err: any) {
      setUnattended((m) => ({ ...m, [pid]: prev }));
      setError(`保存失败: ${err.message}`);
    } finally {
      setUnattendedSaving((m) => ({ ...m, [pid]: false }));
    }
  };

  const handleAdd = async () => {
    const trimmedName = name.trim();
    if (!trimmedName) {
      setError("名称不能为空");
      return;
    }
    let env: Record<string, string> = {};
    const rawEnv = envJson.trim();
    if (rawEnv) {
      try {
        const parsed = JSON.parse(rawEnv) as unknown;
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          throw new Error("not an object");
        }
        env = Object.fromEntries(
          Object.entries(parsed as Record<string, unknown>).map(([k, v]) => [k, String(v)]),
        );
      } catch {
        setError("env JSON 格式无效");
        return;
      }
    }
    setSaving(true);
    setError("");
    try {
      await addMcpServer({
        name: trimmedName,
        transport,
        ...(transport === "http"
          ? { url: endpoint.trim() }
          : {
              command: endpoint.trim(),
              args: argsText
                .split("\n")
                .map((l) => l.trim())
                .filter(Boolean),
            }),
        env,
        enabled: true,
      });
      setName("");
      setEndpoint("");
      setEnvJson("{}");
      await refresh();
    } catch (err: any) {
      setError(`保存失败: ${err.message}`);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (serverName: string) => {
    setPendingDelete(null);
    setError("");
    try {
      await removeMcpServer(serverName);
      await refresh();
    } catch (err: any) {
      setError(`删除失败: ${err.message}`);
    }
  };

  return (
    <>
      <div
        className={`fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-[2px] transition-opacity duration-200 ${entered ? "opacity-100" : "opacity-0"}`}
        onClick={onClose}
      >
      <div
        className={`bg-g-bg border border-g-border rounded-gmLg shadow-gm-lg p-6 w-[30rem] max-w-[90vw] max-h-[85vh] overflow-y-auto transform transition-all duration-200 ease-out ${entered ? "opacity-100 translate-y-0 scale-100" : "opacity-0 translate-y-3 scale-[0.98]"}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2.5 mb-4">
          <span className="w-7 h-7 rounded-gm bg-g-blue-bg flex items-center justify-center text-sm shrink-0">🔌</span>
          <h2 className="text-sm font-semibold text-g-fg">设置</h2>
        </div>

        {/* ── 无人值守模式（按项目）────────────────────────── */}
        <div className="mb-5">
          <p className="text-xs font-medium text-g-fg-2 mb-2">无人值守模式</p>
          <p className="text-xs text-g-fg-3 mb-2 leading-relaxed">
            开启后，该项目 agent 的需审批命令不再等待人工（0 等待拒绝并指路替代方案）。测试项目建议开启。
          </p>
          {unattendedUnknown && (
            <div className="bg-g-yellow-bg border border-g-yellow text-g-yellow px-3 py-1.5 rounded-gm text-xs mb-2">
              设置读取失败——下方开关状态未知，请刷新后操作。
            </div>
          )}
          {globalUnattended && (
            <div className="bg-g-blue-bg text-g-fg-2 px-3 py-1.5 rounded-gm text-xs mb-2">
              全局默认无人值守已开启——项目级「关」在其覆盖下不生效（读端回退全局键）。需彻底关闭请清掉全局键 unattended_mode。
            </div>
          )}
          {projects.length === 0 ? (
            <div className="text-xs text-g-fg-4 py-2">（暂无项目）</div>
          ) : (
            <div className="border border-g-border rounded-gm divide-y divide-g-border/60 max-h-44 overflow-y-auto">
              {projects.map((pr) => (
                <div key={pr.id} className="flex items-center justify-between px-3 py-2">
                  <span className="text-xs text-g-fg truncate mr-3" title={pr.name}>
                    {pr.name}
                  </span>
                  <button
                    onClick={() => toggleUnattended(pr.id)}
                    disabled={!!unattendedSaving[pr.id]}
                    className={`relative w-9 h-5 rounded-full transition-colors shrink-0 disabled:opacity-50 ${
                      unattended[pr.id] ? "bg-g-green-vivid" : "bg-g-border"
                    }`}
                    aria-label={`无人值守 ${pr.name}`}
                  >
                    <span
                      className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${
                        unattended[pr.id] ? "left-[1.15rem]" : "left-0.5"
                      }`}
                    />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ── MCP 服务器 ──────────────────────────────────── */}
        <p className="text-xs font-medium text-g-fg-2 mb-2">MCP 服务器</p>
        <p className="text-xs text-g-fg-3 mb-3 leading-relaxed">
          管理 MCP 服务器配置；Agent 详情面板里可将服务器绑定到具体 Agent。
        </p>

        {error && (
          <div className="bg-g-red-bg border border-g-red text-g-red px-3 py-1.5 rounded-gm text-xs mb-3">
            {error}
            <button onClick={() => setError("")} className="ml-2 text-g-red hover:text-g-red">×</button>
          </div>
        )}

        {/* 服务器列表 */}
        <div className="space-y-1.5 mb-4">
          {loading ? (
            <div className="text-xs text-g-fg-4 py-2">加载中...</div>
          ) : servers.length === 0 ? (
            <div className="text-xs text-g-fg-4 py-2">尚未配置任何 MCP 服务器</div>
          ) : (
            servers.map((s) => (
              <div
                key={s.id || s.name}
                className="flex items-center gap-2 px-3 py-2 rounded-gm border border-g-border/60 bg-g-bg-soft"
              >
                <span className={`w-2 h-2 rounded-full shrink-0 ${s.enabled ? "bg-g-green-vivid" : "bg-g-fg-4"}`} />
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-medium text-g-fg truncate">{s.name}</div>
                  <div className="text-[10px] text-g-fg-4 truncate">
                    {s.transport} · {s.url || s.command || "（未配置端点）"}
                  </div>
                </div>
                <span className={`px-1.5 py-0.5 text-[10px] rounded-gm shrink-0 ${s.enabled ? "bg-g-green-bg text-g-green" : "bg-g-bg-muted text-g-fg-3"}`}>
                  {s.enabled ? "启用" : "禁用"}
                </span>
                <button
                  onClick={() => setPendingDelete(s.name)}
                  title={`删除 ${s.name}`}
                  className="text-g-fg-4 hover:text-g-red transition-colors shrink-0 px-1"
                >
                  ×
                </button>
              </div>
            ))
          )}
        </div>

        {/* 添加表单 */}
        <div className="pt-3 border-t border-g-border/50 space-y-2">
          <div className="text-xs font-medium text-g-fg-3">添加服务器</div>
          <div className="flex gap-2">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="名称"
              className="flex-1 min-w-0 px-3 py-2 text-xs border border-g-border rounded-gm bg-g-bg-soft text-g-fg placeholder-g-fg-4/70 focus:border-g-blue transition-shadow"
            />
            <select
              value={transport}
              onChange={(e) => setTransport(e.target.value as "http" | "stdio")}
              className="w-24 px-2 py-2 text-xs border border-g-border rounded-gm bg-g-bg-soft text-g-fg focus:border-g-blue"
            >
              <option value="http">http</option>
              <option value="stdio">stdio</option>
            </select>
          </div>
          <input
            value={endpoint}
            onChange={(e) => setEndpoint(e.target.value)}
            placeholder={transport === "http" ? "URL（如 http://localhost:8080/mcp）" : "可执行文件（如 npx 或 python）——参数填下方，勿拼整条命令"}
            className="w-full px-3 py-2 text-xs border border-g-border rounded-gm bg-g-bg-soft text-g-fg placeholder-g-fg-4/70 focus:border-g-blue transition-shadow"
          />
          {transport === "stdio" && (
            <textarea
              value={argsText}
              onChange={(e) => setArgsText(e.target.value)}
              rows={2}
              placeholder={"启动参数，每行一个（如：\n-y\n@some/mcp-server）"}
              className="w-full px-3 py-2 text-xs font-mono border border-g-border rounded-gm bg-g-bg-soft text-g-fg placeholder-g-fg-4/70 focus:border-g-blue resize-none transition-shadow"
            />
          )}
          <textarea
            value={envJson}
            onChange={(e) => setEnvJson(e.target.value)}
            rows={2}
            placeholder='env JSON（如 {"API_KEY": "sk-..."}）'
            className="w-full px-3 py-2 text-xs font-mono border border-g-border rounded-gm bg-g-bg-soft text-g-fg placeholder-g-fg-4/70 focus:border-g-blue resize-none transition-shadow"
          />
          <div className="flex justify-end gap-2">
            <button onClick={onClose} className="px-3 py-1.5 text-xs text-g-fg-3 hover:text-g-fg rounded-gm hover:bg-g-bg-muted active:scale-[0.97] transition-all">
              关闭
            </button>
            <button
              onClick={handleAdd}
              disabled={saving}
              className="px-3 py-1.5 text-xs bg-g-blue text-white rounded-gm shadow-gm-sm hover:bg-g-blue active:scale-[0.97] transition-all disabled:opacity-50"
            >
              {saving ? "保存中..." : "添加"}
            </button>
          </div>
        </div>
      </div>
      </div>

      {/* 删除确认（overlay 外层 sibling，避免点击冒泡关闭面板） */}
      {pendingDelete !== null && (
        <ConfirmDialog
          title="删除 MCP 服务器"
          message={`确定删除「${pendingDelete}」？已绑定它的 Agent 将无法再使用其工具。`}
          danger
          onConfirm={() => handleDelete(pendingDelete)}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </>
  );
}
