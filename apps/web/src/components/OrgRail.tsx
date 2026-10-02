import { useEffect, useState } from "react";
import { PanelLeftOpen } from "lucide-react";
import { getOrgTree } from "../api";
import { useAppStore } from "../store";
import { openAgentChat } from "../navigation/commands";
import { deriveAgentDot, deriveHealthError } from "../utils/agentStatus";
import { ROLE_COLORS, sanitizeTree, type OrgNodeData } from "./OrgTree";

/**
 * 组织图折叠后的窄条（2026-10-01「左栏太占地方」修复）。
 *
 * 左 Panel 收起（collapsedSize≈3.5% ≈ 40–56px）时替代页头+画布渲染：
 * 竖排头像 + 合成状态圆点（与组织图节点同一 deriveAgentDot 口径），
 * 点击选中成员并打开对话，顶部按钮展开恢复完整面板。
 *
 * ⚠ 数据：自己拉一份 org tree（orgTreeVersion / 项目切换时刷新）。
 * 组织图本体在此期间保持挂载但 display:none（App.tsx 折叠实现约定），
 * 其 approvals/闹钟轮询不中断；本窄条只读 store 的实时状态
 * （processing / disposition / health），不新增轮询。
 */

type FlatRow = OrgNodeData & { depth: number };

function flatten(nodes: OrgNodeData[], depth = 0, out: FlatRow[] = []): FlatRow[] {
  for (const n of nodes) {
    out.push({ ...n, depth });
    if (n.children?.length) flatten(n.children, depth + 1, out);
  }
  return out;
}

export default function OrgRail({ onExpand }: { onExpand: () => void }) {
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);
  const orgTreeVersion = useAppStore((s) => s.orgTreeVersion);
  const selectedAgentId = useAppStore((s) => s.selectedAgentId);
  const setSelectedAgent = useAppStore((s) => s.setSelectedAgent);
  const processingAgents = useAppStore((s) => s.processingAgents);
  const agentDispositions = useAppStore((s) => s.agentDispositions);
  const agentHealth = useAppStore((s) => s.agentHealth);
  const [rows, setRows] = useState<FlatRow[]>([]);

  useEffect(() => {
    let mounted = true;
    if (!selectedProjectId) {
      setRows([]);
      return;
    }
    (async () => {
      try {
        const data = await getOrgTree(selectedProjectId);
        if (!mounted) return;
        let parsed: OrgNodeData[];
        if (Array.isArray(data)) {
          parsed = data;
        } else if (data && typeof data === "object" && "tree" in data) {
          parsed = (data as { tree: OrgNodeData[] }).tree ?? [];
        } else if (data && typeof data === "object" && "id" in data) {
          parsed = [data as OrgNodeData];
        } else {
          parsed = [];
        }
        setRows(flatten(sanitizeTree(parsed)));
      } catch (err) {
        // 与 OrgTree 同口径 fail-visible：窄条空白时至少留痕可排查（审计 L1）
        console.error("Failed to fetch org tree (rail):", err);
        if (mounted) setRows([]);
      }
    })();
    return () => {
      mounted = false;
    };
  }, [selectedProjectId, orgTreeVersion]);

  const processingSet = new Set(processingAgents);

  return (
    <div className="h-full flex flex-col items-center py-2 gap-1.5 min-w-0" data-testid="org-rail">
      <button
        onClick={onExpand}
        title="展开组织图"
        aria-label="展开组织图"
        className="shrink-0 p-1.5 rounded-gm text-g-fg-4 hover:text-g-fg hover:bg-g-bg-muted transition-colors"
      >
        <PanelLeftOpen className="w-4 h-4" />
      </button>
      <div className="flex-1 w-full overflow-y-auto flex flex-col items-center gap-1.5 pb-2">
        {rows.map((r) => {
          const healthError = deriveHealthError(agentHealth[r.id], selectedProjectId);
          const dot = deriveAgentDot({
            healthError,
            isProcessing: processingSet.has(r.id),
            disposition: agentDispositions[r.id],
            lifecycle: r.status,
          });
          const color = ROLE_COLORS[r.role] || "#6b7280";
          const selected = selectedAgentId === r.id;
          return (
            <button
              key={r.id}
              onClick={() => {
                setSelectedAgent(r.id);
                openAgentChat(r.id);
              }}
              title={`${r.name} · ${dot.title}`}
              // 缩进封顶 3 层：窄条只有 ~40–56px，深层溢出即裁剪（审计 L3）
              style={{ marginLeft: Math.min(r.depth, 3) * 3 }}
              className={`relative shrink-0 rounded-full transition-shadow ${
                selected ? "ring-2 ring-g-blue" : "hover:ring-1 hover:ring-g-border-strong"
              }`}
            >
              <span
                className="w-5 h-5 rounded-full flex items-center justify-center text-[9px] font-semibold"
                style={{ background: `${color}16`, color }}
              >
                {r.name.charAt(0)}
              </span>
              <span
                className={`absolute -bottom-0.5 -right-0.5 w-[7px] h-[7px] rounded-full ring-[1.5px] ring-white ${dot.dotClass}`}
                aria-hidden="true"
              />
            </button>
          );
        })}
      </div>
    </div>
  );
}
