import {
  useEffect, useState, useCallback, useRef, useMemo, useLayoutEffect,
} from "react";
import ApprovalDialog from "./ApprovalDialog";
import { getOrgTree, getCommunications, getProjectPendingApprovals, getUserPings, getProjectAlarms } from "../api";
import { useAppStore, type AgentAlarmInfo } from "../store";
import { getRoleStyle, getPositionLabel } from "../utils/role-styles";
import { realMsToGameSeconds, gameSecondsToRealMs, decomposeGameSeconds } from "../utils/game-time";

// ── Types ──────────────────────────────────────────────────────

interface OrgNodeData {
  id: string;
  name: string;
  role: string;
  position: string;
  status: string;
  children?: OrgNodeData[];
}

interface LayoutNode {
  id: string;
  name: string;
  role: string;
  position: string;
  status: string;
  x: number;
  y: number;
  w: number;
  children?: LayoutNode[];
}

// ── Tree analysis helpers ──────────────────────────────────────

function countNodes(node: OrgNodeData): number {
  let n = 1;
  if (node.children) for (const c of node.children) n += countNodes(c);
  return n;
}

/** BUG-018 fix: Recursively sanitize org-tree data, removing nodes with
 *  missing/empty ids and ensuring all required fields have defaults.
 *  Prevents blank node cards caused by malformed agent records. */
function sanitizeTree(nodes: OrgNodeData[]): OrgNodeData[] {
  const out: OrgNodeData[] = [];
  for (const n of nodes) {
    if (!n || !n.id) continue; // skip malformed entries
    const cleaned: OrgNodeData = {
      id: n.id,
      name: n.name || "?",
      role: n.role || "unknown",
      position: n.position || "",
      status: n.status || "idle",
    };
    if (Array.isArray(n.children) && n.children.length) {
      cleaned.children = sanitizeTree(n.children);
    }
    out.push(cleaned);
  }
  return out;
}

function getMaxDepth(node: OrgNodeData, d = 0): number {
  if (!node.children?.length) return d;
  return Math.max(...node.children.map((c) => getMaxDepth(c, d + 1)));
}

function getMaxBreadth(node: OrgNodeData): number {
  const childCount = node.children?.length ?? 0;
  const childMax = node.children
    ? Math.max(0, ...node.children.map(getMaxBreadth))
    : 0;
  return Math.max(childCount, childMax);
}

// ── Adaptive layout ────────────────────────────────────────────

interface LayoutParams {
  nodeH: number;
  hGap: number;
  vGap: number;
  minW: number;
  maxW: number;
  strokeWidth: number;
}

function computeLayoutParams(roots: OrgNodeData[]): LayoutParams {
  const total = roots.reduce((s, r) => s + countNodes(r), 0);
  const maxDepth = Math.max(0, ...roots.map((r) => getMaxDepth(r)));
  const maxBreadth = Math.max(0, ...roots.map(getMaxBreadth));

  let nodeH = 54, hGap = 20, vGap = 36;
  let minW = 100, maxW = 220;
  let strokeWidth = 1.5;

  if (total > 30 || maxBreadth > 8) {
    // Large / flat tree — compact everything
    nodeH = 44;
    hGap = Math.max(8, 20 - (maxBreadth - 8) * 2);
    vGap = Math.max(18, 36 - (maxDepth - 3) * 3);
    minW = 90; maxW = 170;
    strokeWidth = 1;
  } else if (total > 15 || maxBreadth > 5) {
    // Medium tree — slightly compressed
    nodeH = 48;
    hGap = 14;
    vGap = 28;
    minW = 90; maxW = 190;
    strokeWidth = 1.2;
  }

  return { nodeH, hGap, vGap, minW, maxW, strokeWidth };
}

/** Estimate card width from name + position text. */
function estimateWidth(node: OrgNodeData, minW: number, maxW: number): number {
  let w = 0;
  for (const ch of node.name) w += /[一-鿿]/.test(ch) ? 13 : 7.5;
  if (node.position) w += 48;
  return Math.max(minW, Math.min(maxW, Math.ceil(w + 36)));
}

function layoutTree(
  node: OrgNodeData, depth: number, p: LayoutParams,
): { root: LayoutNode; width: number } {
  const myW = estimateWidth(node, p.minW, p.maxW);
  const y = depth * (p.nodeH + p.vGap);

  if (!node.children?.length) {
    return {
      root: {
        id: node.id, name: node.name, role: node.role,
        position: node.position, status: node.status,
        x: 0, y, w: myW,
      },
      width: myW,
    };
  }

  const childResults = node.children.map((c) => layoutTree(c, depth + 1, p));

  function shiftNode(n: LayoutNode, dx: number): LayoutNode {
    return {
      ...n,
      x: n.x + dx,
      children: n.children?.map((c) => shiftNode(c, dx)),
    };
  }

  // Place child subtrees sequentially left to right. Each child's local
  // bounding box already starts at x = 0 (guaranteed by the recursive call),
  // so placing at `cx` makes its true bbox [cx, cx + child.width].
  let cx = 0;
  const placed: LayoutNode[] = [];
  for (const child of childResults) {
    placed.push(shiftNode(child.root, cx));
    cx += child.width + p.hGap;
  }

  // Compute the TRUE bounding box of all descendants (grandchildren included).
  let bboxLeft = Infinity, bboxRight = -Infinity;
  function scanBbox(n: LayoutNode) {
    bboxLeft = Math.min(bboxLeft, n.x);
    bboxRight = Math.max(bboxRight, n.x + n.w);
    n.children?.forEach(scanBbox);
  }
  placed.forEach(scanBbox);

  // Center the parent node over the children's bounding-box center so the
  // subtree is visually symmetric. parentCenter == childrenCenter.
  const childrenCenter = (bboxLeft + bboxRight) / 2;
  const parentX = childrenCenter - myW / 2;

  // Overall bounding box must include the parent node itself.
  const overallLeft = Math.min(parentX, bboxLeft);
  const overallRight = Math.max(parentX + myW, bboxRight);
  const overallW = overallRight - overallLeft;

  // Shift everything so the overall bounding box starts at x = 0. This is the
  // critical fix: previously the root was pinned at x = 0 while descendants
  // could extend into negative x, so the reported width understated the real
  // extent and caused subtrees to overlap their left sibling at higher levels.
  const dx = -overallLeft;
  const shiftedChildren = placed.map((c) => shiftNode(c, dx));
  const shiftedParentX = parentX + dx;

  return {
    root: {
      id: node.id, name: node.name, role: node.role,
      position: node.position, status: node.status,
      x: shiftedParentX, y, w: myW,
      children: shiftedChildren,
    },
    width: overallW,
  };
}

/** Compute the actual bounding box width/height of a laid-out tree. */
function measureTree(root: LayoutNode, nodeH: number): { w: number; h: number } {
  let minX = Infinity, maxX = -Infinity, maxY = -Infinity;
  function scan(n: LayoutNode) {
    minX = Math.min(minX, n.x);
    maxX = Math.max(maxX, n.x + n.w);
    maxY = Math.max(maxY, n.y + nodeH);
    n.children?.forEach(scan);
  }
  scan(root);
  return { w: maxX - minX, h: maxY };
}

// ── Connectors (orthogonal + rounded corners) ──────────────────

function Connectors({
  parent, children, nodeH, strokeWidth,
}: {
  parent: LayoutNode; children: LayoutNode[]; nodeH: number; strokeWidth: number;
}) {
  if (!children.length) return null;

  const px = parent.x + parent.w / 2;
  const py = parent.y + nodeH;
  const childY = children[0].y;
  const midY = py + (childY - py) * 0.5;
  const r = Math.min(8, (childY - py) * 0.25);

  // Single child — rounded orthogonal connector (matches multi-child style)
  if (children.length === 1) {
    const cx = children[0].x + children[0].w / 2;
    // Directly below — straight vertical line
    if (Math.abs(cx - px) < 0.5) {
      return (
        <svg className="absolute inset-0 pointer-events-none" style={{ overflow: "visible" }}>
          <line
            x1={px} y1={py} x2={cx} y2={childY}
            stroke="#bdc1c6" strokeWidth={strokeWidth} strokeLinecap="round"
          />
        </svg>
      );
    }
    // Clamp radius so the two arcs never overlap when horizontal offset is small
    const horiz = Math.abs(cx - px);
    const cr = Math.min(r, horiz / 2);
    const dir = cx > px ? 1 : -1;
    const sweep = cx > px ? 1 : 0;
    const d =
      `M ${px} ${py} L ${px} ${midY - cr}` +
      ` A ${cr} ${cr} 0 0 ${sweep} ${px + dir * cr} ${midY}` +
      ` L ${cx - dir * cr} ${midY}` +
      ` A ${cr} ${cr} 0 0 ${sweep} ${cx} ${midY + cr}` +
      ` L ${cx} ${childY}`;
    return (
      <svg className="absolute inset-0 pointer-events-none" style={{ overflow: "visible" }}>
        <path
          d={d} fill="none" stroke="#bdc1c6" strokeWidth={strokeWidth}
          strokeLinejoin="round" strokeLinecap="round"
        />
      </svg>
    );
  }

  // Multiple children — trunk + branch with rounded corners
  const xs = children.map((c) => c.x + c.w / 2);
  const x0 = xs[0];
  const xn = xs[xs.length - 1];
  const cr = Math.min(r, (xn - x0) / (children.length * 2));

  // Trunk: parent-bottom → midY, then arc into horizontal bar toward first child
  const trunk =
    `M ${px} ${py} L ${px} ${midY - cr}` +
    ` A ${cr} ${cr} 0 0 1 ${px + cr} ${midY}` +
    ` L ${x0 + cr} ${midY}` +
    ` A ${cr} ${cr} 0 0 0 ${x0} ${midY + cr}` +
    ` L ${x0} ${childY}`;

  // Branch: horizontal bar from first-child zone to last child, with drops
  let branch = `M ${x0 + cr} ${midY}`;
  if (children.length === 2) {
    branch += ` L ${xn - cr} ${midY}`;
  } else {
    for (let i = 1; i < children.length - 1; i++) {
      branch += ` L ${xs[i]} ${midY}`;
    }
    branch += ` L ${xn - cr} ${midY}`;
  }
  branch +=
    ` A ${cr} ${cr} 0 0 1 ${xn} ${midY + cr}` +
    ` L ${xn} ${childY}`;

  // Intermediate child drops (children 1..n-2)
  const drops = children.slice(1, -1).map((c, i) => {
    const cx = xs[i + 1];
    return (
      <line
        key={c.id}
        x1={cx} y1={midY} x2={cx} y2={childY}
        stroke="#cbc0aa" strokeWidth={strokeWidth}
      />
    );
  });

  return (
    <svg className="absolute inset-0 pointer-events-none" style={{ overflow: "visible" }}>
      <path
        d={trunk} fill="none" stroke="#bdc1c6"
        strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round"
      />
      <path
        d={branch} fill="none" stroke="#cbc0aa"
        strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round"
      />
      {drops}
    </svg>
  );
}

// ── Node card ──────────────────────────────────────────────────

const ROLE_COLORS: Record<string, string> = {
  ceo: "#f59e0b", hr: "#f43f5e", architect: "#a855f7",
  manager: "#3b82f6", developer: "#22c55e", module_dev: "#22c55e",
  test_engineer: "#eab308", qa: "#eab308", code_reviewer: "#6366f1",
  security_auditor: "#ef4444", web_perf_auditor: "#06b6d4",
  devops: "#06b6d4",
};

/** Format a real-time millisecond countdown into a compact label, e.g. "30秒", "5分12秒", "2时15分". */
function formatAlarmCountdown(realMs: number): string {
  if (realMs <= 0) return "即将";
  const totalSec = Math.floor(realMs / 1000);
  const d = Math.floor(totalSec / 86400);
  const h = Math.floor((totalSec % 86400) / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (d > 0) return `${d}天${h}时`;
  if (h > 0) return `${h}时${m}分`;
  if (m > 0) return `${m}分${s}秒`;
  return `${s}秒`;
}

function TreeNodeCard({
  node, isSelected, onSelect, onAddChild, onApproval, onToggle,
  expanded, pendingCount, hasUserPing, isProcessing, isActive, nodeH, alarm,
}: {
  node: LayoutNode;
  isSelected: boolean;
  onSelect: (id: string) => void;
  onAddChild: (parentId: string) => void;
  onApproval: (id: string) => void;
  onToggle: () => void;
  expanded: boolean;
  pendingCount: number;
  hasUserPing: boolean;
  isProcessing: boolean;
  isActive: boolean;
  nodeH: number;
  alarm?: AgentAlarmInfo;
}) {
  const compact = nodeH < 42;
  const hasChildren = !!node.children?.length;
  const roleStyle = getRoleStyle(node.role);
  const positionLabel = getPositionLabel(node.position, node.role);
  const accentColor = ROLE_COLORS[node.role] || "#6b7280";

  // Live countdown tick — only runs while this agent has a pending alarm.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!alarm) return;
    setNow(Date.now());
    const i = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(i);
  }, [alarm]);

  // Extrapolate current game time from the sampled snapshot, then compute the
  // remaining real time until the alarm fires.
  let alarmLabel: string | null = null;
  let alarmTitle = "";
  if (alarm) {
    const elapsedGameSec = realMsToGameSeconds(now - alarm.sampledAt);
    const currentGameSec = alarm.currentGameSeconds + elapsedGameSec;
    const remainingGameSec = alarm.fireAtGameSeconds - currentGameSec;
    const remainingRealMs = gameSecondsToRealMs(remainingGameSec);
    alarmLabel = formatAlarmCountdown(remainingRealMs);
    const gt = decomposeGameSeconds(Math.max(0, remainingGameSec));
    alarmTitle = `闹钟：${alarm.purpose}\n剩余游戏时间：${gt.day}天${gt.hours}时${gt.minutes}分`;
  }

  return (
    <div
      onClick={(e) => { e.stopPropagation(); onSelect(node.id); }}
      data-interactive="true"
      className={[
        "absolute cursor-pointer rounded-gm overflow-hidden",
        "transition-colors duration-150 ease-out group",
        isSelected
          ? "ring-1 ring-g-blue/50"
          : node.role === "ceo"
            ? "ring-1 ring-g-blue/30 hover:border-g-blue/40"
            : "hover:border-g-blue/40",
      ].join(" ")}
      style={{
        left: node.x,
        top: node.y,
        width: node.w,
        minHeight: nodeH,
        borderLeft: `3px solid ${accentColor}`,
        borderTop: "1px solid #ebebeb",
        borderRight: "1px solid #ebebeb",
        borderBottom: "1px solid #ebebeb",
        ...(isProcessing ? {
          borderTopColor: "rgba(52,168,83,.30)",
          borderRightColor: "rgba(52,168,83,.30)",
          borderBottomColor: "rgba(52,168,83,.30)",
        } : {}),
        background: isSelected
          ? "rgba(66,133,244,0.08)"
          : "rgba(255,255,255,1)",
        boxShadow: isSelected
          ? "0 0 0 1px rgba(66,133,244,.25), 0 2px 8px rgba(60,64,67,.18)"
          : node.role === "ceo"
            ? "0 1px 4px rgba(66,133,244,.15)"
            : isActive && isProcessing
              ? "0 1px 3px rgba(60,64,67,.20)"
              : "0 1px 1px rgba(60,64,67,.10)",
        // Re-rasterize text crisply at any CSS scale level
        textRendering: "optimizeLegibility",
        WebkitFontSmoothing: "antialiased",
      }}
    >
      {/* Row 1: status dot + name + expand + ping */}
      <div className={`flex items-center ${compact ? "gap-0.5 px-1 py-0.5" : "gap-1 px-1.5 py-0.5"}`}>
        <span
          className={`rounded-full shrink-0 ${compact ? "w-1 h-1" : "w-1.5 h-1.5"} ${
            node.status === "active"
              ? isProcessing ? "bg-emerald-400 animate-pulse" : "bg-g-fg-4"
              : node.status === "idle" || node.status === "inactive" ? "bg-g-fg-4"
              : node.status === "promoted" ? "bg-blue-400"
              : node.status === "receiving" ? "bg-amber-400 animate-pulse"
              : node.status === "merging" ? "bg-purple-400 animate-pulse"
              : node.status === "dissolving" || node.status === "archived" ? "bg-red-600"
              : "bg-gray-500"
          }`}
        />
        <span
          className={`font-medium truncate ${compact ? "text-[10px]" : "text-xs"} ${
            isSelected ? "text-g-fg" : "text-g-fg"
          }`}
        >
          {node.name}
        </span>
        {hasChildren && (
          <span
            onClick={(e) => { e.stopPropagation(); onToggle(); }}
            className="text-g-fg-3 hover:text-g-fg cursor-pointer shrink-0 ml-0.5 flex items-center"
          >
            <svg className={compact ? "w-2.5 h-2.5" : "w-3 h-3"} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
              <path strokeLinecap="round" strokeLinejoin="round" d={expanded ? "M19 9l-7 7-7-7" : "M9 5l7 7-7 7"} />
            </svg>
          </span>
        )}
        <span className="flex-1" />
        {hasUserPing && (
          <span className={`${compact ? "w-1 h-1" : "w-1.5 h-1.5"} bg-red-500 rounded-full animate-pulse shrink-0`} />
        )}
      </div>

      {/* Row 2: position badge + pending */}
      <div className={`flex items-center ${compact ? "gap-0.5 px-1 pb-0.5" : "gap-1 px-1.5 pb-0.5"}`}>
        {positionLabel ? (
          <span
            className={`font-medium rounded truncate ${compact ? "text-[8px] px-1" : "text-[10px] px-1.5 py-px"}`}
            style={{
              background: `${accentColor}18`,
              color: accentColor,
            }}
          >
            {positionLabel}
          </span>
        ) : <span />}
        <span className="flex-1" />
        {alarmLabel && (
          <span
            title={alarmTitle}
            className={`shrink-0 font-medium bg-g-blue-bg text-g-blue rounded leading-none flex items-center gap-0.5 ${
              compact ? "text-[8px] px-0.5 py-px" : "text-[10px] px-1 py-0.5"
            }`}
          >
            <svg className={compact ? "w-1.5 h-1.5" : "w-2 h-2"} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            {alarmLabel}
          </span>
        )}
        {pendingCount > 0 && (
          <span
            onClick={(e) => { e.stopPropagation(); onApproval(node.id); }}
            className={`shrink-0 font-medium bg-g-yellow-bg text-amber-700 rounded cursor-pointer hover:bg-amber-500/30 leading-none ${
              compact ? "text-[8px] px-0.5 py-px" : "text-[10px] px-1 py-0.5"
            }`}
          >
            {pendingCount}
          </span>
        )}
      </div>

      {/* Add child — absolutely positioned, does NOT affect row layout */}
      <span
        onClick={(e) => { e.stopPropagation(); onAddChild(node.id); }}
        className="absolute bottom-0.5 right-0.5 rounded hidden group-hover:flex items-center justify-center text-g-fg-3 hover:text-g-blue hover:bg-g-blue/10"
        style={{ width: compact ? 14 : 16, height: compact ? 14 : 16 }}
      >
        <svg
          className={compact ? "w-2 h-2" : "w-2.5 h-2.5"}
          fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
        </svg>
      </span>
    </div>
  );
}

// ── Zoom controls ──────────────────────────────────────────────

function ZoomControls({
  scale, onZoomIn, onZoomOut, onFit,
}: {
  scale: number;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onFit: () => void;
}) {
  return (
    <div
      data-interactive="true"
      onPointerDown={(e) => e.stopPropagation()}
      className="absolute bottom-3 right-3 flex items-center gap-0.5 bg-white/90 backdrop-blur-sm rounded-lg border border-g-border p-0.5 z-20"
    >
      {[
        { label: "−", onClick: onZoomOut, title: "缩小" },
        null, // divider
        { label: `${Math.round(scale * 100)}%`, onClick: onFit, title: "适应屏幕", isText: true },
        null,
        { label: "+", onClick: onZoomIn, title: "放大" },
        null,
        { label: "⊡", onClick: onFit, title: "适应屏幕" },
      ].map((btn, i) =>
        btn === null ? (
          <div key={i} className="w-px h-4 bg-g-border" />
        ) : (
          <button
            key={i}
            onClick={btn.onClick}
            title={btn.title}
            className={`rounded-md text-g-fg-3 hover:text-g-fg hover:bg-g-bg-soft transition-colors flex items-center justify-center ${
              "isText" in btn && btn.isText
                ? "text-[10px] px-1.5 py-1 min-w-[40px] font-mono"
                : "w-7 h-7 text-sm"
            }`}
          >
            {btn.label}
          </button>
        ),
      )}
    </div>
  );
}

// ── OrgTree ────────────────────────────────────────────────────

const INNER_PAD = 40; // CSS padding on the inner layout div

function OrgTree() {
  // Store
  const orgTreeVersion = useAppStore((s) => s.orgTreeVersion);
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);
  const setActiveCommunications = useAppStore((s) => s.setActiveCommunications);
  const setAllPendingApprovals = useAppStore((s) => s.setAllPendingApprovals);
  const setUserPingAgentIds = useAppStore((s) => s.setUserPingAgentIds);
  const setAgentAlarms = useAppStore((s) => s.setAgentAlarms);
  const selectedAgentId = useAppStore((s) => s.selectedAgentId);
  const setSelectedAgent = useAppStore((s) => s.setSelectedAgent);
  const setRightPanelTab = useAppStore((s) => s.setRightPanelTab);
  const openAddAgent = useAppStore((s) => s.openAddAgent);
  const pendingApprovals = useAppStore((s) => s.pendingApprovals);
  const userPingAgentIds = useAppStore((s) => s.userPingAgentIds);
  const processingAgents = useAppStore((s) => s.processingAgents);
  const agentAlarms = useAppStore((s) => s.agentAlarms);

  // State
  const [roots, setRoots] = useState<OrgNodeData[]>([]);
  const [approvalAgentId, setApprovalAgentId] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  // Canvas transform
  const [transform, setTransform] = useState({ scale: 1, tx: 0, ty: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const dragRef = useRef<{ startX: number; startY: number; startTx: number; startTy: number } | null>(null);

  const containerRef = useRef<HTMLDivElement>(null);
  const fittedRef = useRef<string | null>(null);

  // ── Data fetching ──

  useEffect(() => {
    let mounted = true;
    async function fetchTree() {
      if (!selectedProjectId) { setRoots([]); return; }
      try {
        const data = await getOrgTree(selectedProjectId);
        if (!mounted) return;
        let parsed: OrgNodeData[];
        if (Array.isArray(data)) {
          parsed = data.filter((n: any) => n && n.id);
        } else if (data && typeof data === "object" && "tree" in data) {
          parsed = (data.tree as any[]).filter((n: any) => n && n.id);
        } else if (data && typeof data === "object" && "id" in data) {
          parsed = [data as OrgNodeData];
        } else {
          parsed = [];
        }
        // BUG-018: sanitize tree data before setting state
        const sanitized = sanitizeTree(parsed);
        setRoots(sanitized);
        // Reset fit flag so auto-fit triggers for new data
        fittedRef.current = null;
        // Auto-select CEO
        if (sanitized.length > 0 && !selectedAgentId) {
          const ceo = findCEO(sanitized);
          if (ceo) setSelectedAgent(ceo.id);
        }
      } catch (err: any) {
        // BUG-018 修复：AbortError 静默忽略，不清空 roots
        if (err?.name === "AbortError" || err?._aborted) return;
        console.error("Failed to fetch org tree:", err);
        if (mounted) setRoots([]);
      }
    }
    fetchTree();
    return () => { mounted = false; };
  }, [orgTreeVersion, selectedProjectId]);

  function findCEO(nodes: OrgNodeData[]): OrgNodeData | null {
    for (const n of nodes) {
      if (n.role === "ceo") return n;
      if (n.children) {
        const found = findCEO(n.children);
        if (found) return found;
      }
    }
    return null;
  }

  // Combined polling: communications + approvals + user pings + alarms
  // BUG-027/005 fix: merge 3 separate useEffect polling loops into 1 to
  // reduce React hook pressure and HMR deps-array instability.
  // Jitter each poll cycle randomly (3000 ± 400ms) so 3s-interval endpoints
  // don't pile up synchronously.
  useEffect(() => {
    if (!selectedProjectId) { setAgentAlarms({}); return; }
    const projectId = selectedProjectId; // non-null after guard
    let mounted = true;

    async function pollComms() {
      try {
        const comms = await getCommunications({ projectId });
        if (mounted) setActiveCommunications(comms);
        const approvals = await getProjectPendingApprovals(projectId);
        if (mounted) setAllPendingApprovals(approvals);
      } catch { /* ignore transient network errors */ }
    }

    async function pollPings() {
      try {
        const pings = await getUserPings({ projectId });
        if (!mounted || !Array.isArray(pings)) return;
        const ids = new Set<string>();
        for (const p of pings) {
          if (Array.isArray(p.agentIds)) p.agentIds.forEach((id) => ids.add(id));
          else if (p.agentId) ids.add(p.agentId);
        }
        setUserPingAgentIds([...ids]);
      } catch { /* ignore */ }
    }

    async function pollAlarms() {
      try {
        const data = await getProjectAlarms(projectId);
        if (!mounted) return;
        const map: Record<string, AgentAlarmInfo> = {};
        for (const a of data.alarms) {
          const ex = map[a.toAgentId];
          if (!ex || a.fireAtGameSeconds < ex.fireAtGameSeconds) {
            map[a.toAgentId] = {
              purpose: a.purpose,
              fireAtGameSeconds: a.fireAtGameSeconds,
              currentGameSeconds: data.currentGameSeconds,
              sampledAt: data.realTimestamp,
            };
          }
        }
        setAgentAlarms(map);
      } catch { /* ignore */ }
    }

    const jitter = () => 3000 + Math.random() * 800 - 400; // 2600–3400ms

    // Initial polls staggered by 200ms to avoid thundering-herd
    pollComms();
    const t1 = setTimeout(() => pollPings(), 200);
    const t2 = setTimeout(() => pollAlarms(), 400);

    let commsTimer: ReturnType<typeof setTimeout>;
    let pingsTimer: ReturnType<typeof setTimeout>;
    let alarmsTimer: ReturnType<typeof setTimeout>;

    function scheduleComms() { commsTimer = setTimeout(() => { pollComms(); scheduleComms(); }, jitter()); }
    function schedulePings() { pingsTimer = setTimeout(() => { pollPings(); schedulePings(); }, jitter()); }
    function scheduleAlarms() { alarmsTimer = setTimeout(() => { pollAlarms(); scheduleAlarms(); }, jitter()); }

    scheduleComms();
    schedulePings();
    scheduleAlarms();

    return () => {
      mounted = false;
      clearTimeout(t1); clearTimeout(t2);
      clearTimeout(commsTimer); clearTimeout(pingsTimer); clearTimeout(alarmsTimer);
    };
  }, [selectedProjectId]);

  const handleSelect = useCallback((id: string) => {
    setSelectedAgent(id);
    // Auto-switch from Goals tab to Chat tab when an agent is selected
    const currentTab = useAppStore.getState().rightPanelTab;
    if (currentTab === "goals") {
      setRightPanelTab("chat");
    }
  }, [setSelectedAgent, setRightPanelTab]);

  // ── Layout computation ──

  const params = useMemo(() => computeLayoutParams(roots), [roots]);

  const layoutData = useMemo(() => {
    if (!roots.length) return { centeredRoots: [], maxW: 0, maxH: 0 };
    const results = roots.map((r) => layoutTree(r, 0, params));
    const laidOutRoots = results.map((l) => l.root);

    // Normalize: shift all nodes so minX = 0 (layoutTree may produce negative x)
    const normalized = laidOutRoots.map((r) => {
      let minX = Infinity;
      (function scanMin(n: LayoutNode) {
        minX = Math.min(minX, n.x);
        n.children?.forEach(scanMin);
      })(r);
      if (Math.abs(minX) < 0.5) return r;
      function shift(n: LayoutNode): LayoutNode {
        return { ...n, x: n.x - minX, children: n.children?.map(shift) };
      }
      return shift(r);
    });

    // Measure the laid-out trees
    const measures = normalized.map((r) => measureTree(r, params.nodeH));
    const maxW = Math.max(...measures.map((m) => m.w));
    const maxH = Math.max(...measures.map((m) => m.h));

    // Center each root's subtree within the overall maxW
    const centered = normalized.map((r, i) => {
      const off = (maxW - measures[i].w) / 2;
      if (Math.abs(off) < 0.5) return r;
      function shift(n: LayoutNode): LayoutNode {
        return { ...n, x: n.x + off, children: n.children?.map(shift) };
      }
      return shift(r);
    });

    return { centeredRoots: centered, maxW, maxH };
  }, [roots, params]);

  // Visible nodes (respect collapsed)
  const visible = useMemo(() => {
    const out: LayoutNode[] = [];
    function walk(n: LayoutNode) {
      out.push(n);
      if (n.children && !collapsed.has(n.id)) n.children.forEach(walk);
    }
    layoutData.centeredRoots.forEach(walk);
    return out;
  }, [layoutData.centeredRoots, collapsed]);

  // Connector pairs
  const connectors = useMemo(() => {
    const out: { parent: LayoutNode; children: LayoutNode[] }[] = [];
    function walk(n: LayoutNode) {
      if (n.children && !collapsed.has(n.id)) {
        out.push({ parent: n, children: n.children });
        n.children.forEach(walk);
      }
    }
    layoutData.centeredRoots.forEach(walk);
    return out;
  }, [layoutData.centeredRoots, collapsed]);

  // Bounding box of all visible nodes
  const bounds = useMemo(() => {
    if (!visible.length) return null;
    let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    for (const n of visible) {
      x1 = Math.min(x1, n.x);
      y1 = Math.min(y1, n.y);
      x2 = Math.max(x2, n.x + n.w);
      y2 = Math.max(y2, n.y + params.nodeH);
    }
    return { x1, y1, x2, y2 };
  }, [visible, params.nodeH]);

  // ── Pan / Zoom ──

  const computeFitTransform = useCallback(() => {
    const el = containerRef.current;
    if (!el || !bounds) return null;
    const vw = el.clientWidth;
    const vh = el.clientHeight;
    // Defer fit if container isn't ready yet (initial render race condition)
    if (vw === 0 || vh === 0) return null;
    const pad = 48; // visual margin around the tree
    const bw = bounds.x2 - bounds.x1;
    const bh = bounds.y2 - bounds.y1;
    if (bw <= 0 || bh <= 0) return null;

    // Scale: fit tree (with margin) into viewport, clamped to [0.5, 1.5]
    const scale = Math.max(0.5, Math.min(
      (vw - pad * 2) / bw,
      (vh - pad * 2) / bh,
      1.5,
    ));
    const cx = (bounds.x1 + bounds.x2) / 2;
    const cy = (bounds.y1 + bounds.y2) / 2;

    // Translation: screen_pos = tx + (INNER_PAD + layout_pos) * scale
    // For centering: tx + (INNER_PAD + cx) * scale = vw/2
    return {
      scale,
      tx: vw / 2 - (INNER_PAD + cx) * scale,
      ty: vh / 2 - (INNER_PAD + cy) * scale,
    };
  }, [bounds]);

  // Auto-fit on initial load and when tree structure changes (but not on collapse/expand)
  const treeW = layoutData.maxW;
  const treeH = layoutData.maxH;
  useLayoutEffect(() => {
    if (!roots.length) return;
    // Use rAF to defer fit until layout has settled (avoids container size race condition)
    const rafId = requestAnimationFrame(() => {
      const t = computeFitTransform();
      if (t) setTransform(t);
    });
    return () => cancelAnimationFrame(rafId);
  }, [roots.length, selectedProjectId, treeW, treeH]); // eslint-disable-line react-hooks/exhaustive-deps

  // ResizeObserver: re-fit when container size changes (e.g. panel animation, window resize)
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    let prevW = el.clientWidth;
    let prevH = el.clientHeight;
    const ro = new ResizeObserver(() => {
      const w = el.clientWidth;
      const h = el.clientHeight;
      // Only re-fit if size actually changed significantly (avoid micro-triggers)
      if (Math.abs(w - prevW) > 2 || Math.abs(h - prevH) > 2) {
        prevW = w;
        prevH = h;
        const t = computeFitTransform();
        if (t) setTransform(t);
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [computeFitTransform]);

  // Wheel zoom (non-passive to allow preventDefault)
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;

      setTransform((prev) => {
        const wx = (cx - prev.tx) / prev.scale - INNER_PAD;
        const wy = (cy - prev.ty) / prev.scale - INNER_PAD;
        const factor = e.deltaY > 0 ? 0.92 : 1.08;
        const ns = Math.max(0.3, Math.min(3, prev.scale * factor));
        return { scale: ns, tx: cx - (INNER_PAD + wx) * ns, ty: cy - (INNER_PAD + wy) * ns };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  // Pointer drag (pan)
  const handlePointerDown = useCallback((e: React.PointerEvent) => {
    if ((e.target as HTMLElement).closest("[data-interactive]")) return;
    setIsDragging(true);
    dragRef.current = {
      startX: e.clientX, startY: e.clientY,
      startTx: transform.tx, startTy: transform.ty,
    };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }, [transform.tx, transform.ty]);

  const handlePointerMove = useCallback((e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    setTransform((prev) => ({
      ...prev,
      tx: d.startTx + (e.clientX - d.startX),
      ty: d.startTy + (e.clientY - d.startY),
    }));
  }, []);

  const handlePointerUp = useCallback(() => {
    dragRef.current = null;
    setIsDragging(false);
  }, []);

  const fitToView = useCallback(() => {
    const t = computeFitTransform();
    if (t) setTransform(t);
  }, [computeFitTransform]);

  const zoomIn = useCallback(() => {
    setTransform((p) => {
      const el = containerRef.current;
      if (!el) return p;
      const cx = el.clientWidth / 2;
      const cy = el.clientHeight / 2;
      const wx = (cx - p.tx) / p.scale - INNER_PAD;
      const wy = (cy - p.ty) / p.scale - INNER_PAD;
      const ns = Math.min(3, p.scale * 1.25);
      return { scale: ns, tx: cx - (INNER_PAD + wx) * ns, ty: cy - (INNER_PAD + wy) * ns };
    });
  }, []);

  const zoomOut = useCallback(() => {
    setTransform((p) => {
      const el = containerRef.current;
      if (!el) return p;
      const cx = el.clientWidth / 2;
      const cy = el.clientHeight / 2;
      const wx = (cx - p.tx) / p.scale - INNER_PAD;
      const wy = (cy - p.ty) / p.scale - INNER_PAD;
      const ns = Math.max(0.3, p.scale * 0.8);
      return { scale: ns, tx: cx - (INNER_PAD + wx) * ns, ty: cy - (INNER_PAD + wy) * ns };
    });
  }, []);

  // ── Render ──

  return (
    <div
      ref={containerRef}
      className={`w-full h-full overflow-hidden relative select-none ${
        isDragging ? "cursor-grabbing" : "cursor-grab"
      }`}
      style={{ touchAction: "none" }}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
    >
      {roots.length === 0 ? (
        <div className="flex items-center justify-center h-full text-g-fg-3 text-sm">
          暂无组织成员
        </div>
      ) : (
        <>
          <div
            className="absolute"
            style={{
              transform: `translate3d(${Math.round(transform.tx)}px, ${Math.round(transform.ty)}px, 0) scale(${transform.scale})`,
              transformOrigin: "0 0",
              // NOTE: Do NOT add will-change/backface-visibility here.
              // They force a GPU compositing layer with a cached raster, so
              // zooming in (scale > 1) stretches that bitmap and text blurs.
            }}
          >
            <div
              className="relative"
              style={{
                width: Math.max(layoutData.maxW + INNER_PAD * 2, 400),
                height: Math.max(layoutData.maxH + INNER_PAD * 2, 300),
                padding: INNER_PAD,
              }}
            >
              <div
                className="relative"
                style={{ width: layoutData.maxW, height: layoutData.maxH }}
              >
                {/* Connectors */}
                {connectors.map(({ parent, children }) => (
                  <Connectors
                    key={parent.id}
                    parent={parent}
                    children={children}
                    nodeH={params.nodeH}
                    strokeWidth={params.strokeWidth}
                  />
                ))}

                {/* Node cards */}
                {visible.map((n) => (
                  <TreeNodeCard
                    key={n.id}
                    node={n}
                    isSelected={selectedAgentId === n.id}
                    onSelect={handleSelect}
                    onAddChild={openAddAgent}
                    onApproval={(id) => setApprovalAgentId(id)}
                    onToggle={() => {
                      setCollapsed((prev) => {
                        const next = new Set(prev);
                        if (next.has(n.id)) next.delete(n.id);
                        else next.add(n.id);
                        return next;
                      });
                    }}
                    expanded={!collapsed.has(n.id)}
                    pendingCount={(pendingApprovals[n.id] || []).length}
                    hasUserPing={
                      Array.isArray(userPingAgentIds) && userPingAgentIds.includes(n.id)
                    }
                    isProcessing={processingAgents.includes(n.id)}
                    isActive={n.status === "active"}
                    nodeH={params.nodeH}
                    alarm={agentAlarms[n.id]}
                  />
                ))}
              </div>
            </div>
          </div>

          {/* Zoom controls */}
          <ZoomControls
            scale={transform.scale}
            onZoomIn={zoomIn}
            onZoomOut={zoomOut}
            onFit={fitToView}
          />
        </>
      )}

      {approvalAgentId && (
        <ApprovalDialog agentId={approvalAgentId} onClose={() => setApprovalAgentId(null)} />
      )}
    </div>
  );
}

export default OrgTree;
