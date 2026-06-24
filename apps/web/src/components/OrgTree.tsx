import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeTypes,
  type OnNodesChange,
  MarkerType,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import AgentNode from "./AgentNode";
import ApprovalDialog from "./ApprovalDialog";
import { getOrgTree, getCommunications, getProjectPendingApprovals, getUserPings } from "../api";
import { useAppStore } from "../store";

interface OrgNodeData {
  id: string;
  name: string;
  role: string;
  status: string;
  children?: OrgNodeData[];
}

// Tree layout constants
const NODE_WIDTH = 200;
const NODE_HEIGHT = 80;
const X_SPACING = 250;
const Y_SPACING = 150;

// Custom node types
const nodeTypes: NodeTypes = {
  agent: AgentNode,
};

// Recursive function to compute subtree width
function findCeoInTree(nodes: OrgNodeData[]): OrgNodeData | null {
  for (const n of nodes) {
    if (n.role?.toLowerCase() === "ceo") return n;
    if (n.children?.length) {
      const found = findCeoInTree(n.children);
      if (found) return found;
    }
  }
  return null;
}

function getSubtreeWidth(node: OrgNodeData): number {
  if (!node.children || node.children.length === 0) {
    return NODE_WIDTH;
  }
  const childrenWidth = node.children.reduce(
    (sum, child) => sum + getSubtreeWidth(child),
    0
  );
  const gapWidth = (node.children.length - 1) * (X_SPACING - NODE_WIDTH);
  return Math.max(NODE_WIDTH, childrenWidth + gapWidth);
}

// Recursive function to layout tree and create nodes/edges
function layoutTree(
  node: OrgNodeData,
  x: number,
  y: number,
  nodes: Node[],
  edges: Edge[],
  parentId?: string,
  onApprovalClick?: (agentId: string) => void,
): void {
  // Add current node
  nodes.push({
    id: node.id,
    type: "agent",
    position: { x, y },
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
    data: {
      name: node.name,
      role: node.role,
      status: node.status,
      onApprovalClick,
    },
  });

  // Add edge from parent
  if (parentId) {
    edges.push({
      id: `${parentId}-${node.id}`,
      source: parentId,
      target: node.id,
      type: "smoothstep",
      style: { stroke: "#2a2d3a", strokeWidth: 2 },
      animated: false,
    });
  }

  // Layout children
  if (node.children && node.children.length > 0) {
    const subtreeWidth = getSubtreeWidth(node);
    let childX = x - subtreeWidth / 2 + NODE_WIDTH / 2;
    const childY = y + Y_SPACING;

    for (const child of node.children) {
      const childSubtreeWidth = getSubtreeWidth(child);
      const childCenterX = childX + childSubtreeWidth / 2 - NODE_WIDTH / 2;
      layoutTree(child, childCenterX, childY, nodes, edges, node.id, onApprovalClick);
      childX += childSubtreeWidth + (X_SPACING - NODE_WIDTH);
    }
  }
}

function OrgTree() {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const orgTreeVersion = useAppStore((s) => s.orgTreeVersion);
  const selectedProjectId = useAppStore((s) => s.selectedProjectId);
  const activeCommunications = useAppStore((s) => s.activeCommunications);
  const setActiveCommunications = useAppStore((s) => s.setActiveCommunications);
  const setAllPendingApprovals = useAppStore((s) => s.setAllPendingApprovals);
  const setUserPingAgentIds = useAppStore((s) => s.setUserPingAgentIds);
  const selectedAgentId = useAppStore((s) => s.selectedAgentId);
  const setSelectedAgent = useAppStore((s) => s.setSelectedAgent);
  const baseEdgesRef = useRef<Edge[]>([]);

  // Approval dialog state
  const [approvalAgentId, setApprovalAgentId] = useState<string | null>(null);

  // Callback for when an approval bell is clicked
  const handleApprovalClick = useCallback((agentId: string) => {
    setApprovalAgentId(agentId);
  }, []);

  // Fetch and layout org tree
  useEffect(() => {
    async function fetchTree() {
      const layoutRoots = (roots: OrgNodeData[]) => {
        const newNodes: Node[] = [];
        const newEdges: Edge[] = [];

        let offsetX = 0;
        for (const root of roots) {
          const w = getSubtreeWidth(root);
          layoutTree(root, offsetX + w / 2 - NODE_WIDTH / 2, 0, newNodes, newEdges, undefined, handleApprovalClick);
          offsetX += w + X_SPACING;
        }

        baseEdgesRef.current = newEdges;
        setNodes(newNodes);
        setEdges(newEdges);
      };

      try {
        const data = await getOrgTree(selectedProjectId || undefined);

        // API returns an array of root nodes: [rootAgent, ...]
        let roots: OrgNodeData[];
        if (Array.isArray(data)) {
          roots = data.filter((n: any) => n && n.id);
        } else if (data?.root && data.root.id) {
          roots = [data.root];
        } else if (data?.id) {
          roots = [data];
        } else {
          roots = [];
        }

        // Empty tree is valid — project may have no agents yet
        if (roots.length === 0) {
          baseEdgesRef.current = [];
          setNodes([]);
          setEdges([]);
          return;
        }
        layoutRoots(roots);
        if (!selectedAgentId) {
          const ceoNode = findCeoInTree(roots);
          if (ceoNode) setSelectedAgent(ceoNode.id);
        }
      } catch (err) {
        console.error("Failed to fetch org tree:", err);
        // Show empty tree on error rather than demo data with fake IDs
        baseEdgesRef.current = [];
        setNodes([]);
        setEdges([]);
      }
    }

    fetchTree();
  }, [setNodes, setEdges, orgTreeVersion, selectedProjectId, handleApprovalClick, selectedAgentId, setSelectedAgent]);

  // Poll for active communications and update edges
  useEffect(() => {
    let mounted = true;

    async function pollCommunications() {
      try {
        const comms = await getCommunications();
        if (mounted) {
          setActiveCommunications(comms);
        }
      } catch (err) {
        console.error("Failed to fetch communications:", err);
      }
    }

    pollCommunications();
    const interval = setInterval(pollCommunications, 3000);

    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [setActiveCommunications]);

  // Poll for pending approval requests
  useEffect(() => {
    if (!selectedProjectId) return;
    let mounted = true;

    async function pollApprovals() {
      try {
        const approvals = await getProjectPendingApprovals(selectedProjectId!);
        if (mounted) {
          setAllPendingApprovals(approvals);
        }
      } catch (err) {
        console.error("Failed to fetch pending approvals:", err);
      }
    }

    pollApprovals();
    const interval = setInterval(pollApprovals, 3000);

    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [selectedProjectId, setAllPendingApprovals]);

  // Poll for user pings (agents that sent user-directed messages)
  useEffect(() => {
    let mounted = true;

    async function pollUserPings() {
      try {
        const data = await getUserPings();
        if (mounted) {
          setUserPingAgentIds(data?.agentIds || []);
        }
      } catch (err) {
        console.error("Failed to fetch user pings:", err);
      }
    }

    pollUserPings();
    const interval = setInterval(pollUserPings, 3000);

    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, [setUserPingAgentIds]);

  // Merge communication edges with structural edges
  useEffect(() => {
    const commEdges: Edge[] = activeCommunications.map((comm) => {
      const colorMap: Record<string, string> = {
        dispatch: "#6c8cff",
        message: "#10b981",
        trigger: "#f59e0b",
        peer: "#22d3ee",
      };
      const color = colorMap[comm.type] || "#10b981";
      return {
        id: `comm-${comm.id}`,
        source: comm.fromAgentId,
        target: comm.toAgentId,
        type: "smoothstep",
        animated: true,
        style: {
          stroke: color,
          strokeWidth: 3,
          strokeDasharray: comm.type === "peer" ? "4 4" : "8 4",
        },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color,
          width: 15,
          height: 15,
        },
        zIndex: 1000,
      };
    });

    setEdges([...baseEdgesRef.current, ...commEdges]);
  }, [activeCommunications, setEdges]);

  const defaultEdgeOptions = useMemo(
    () => ({
      type: "smoothstep",
      style: { stroke: "#2a2d3a", strokeWidth: 2 },
    }),
    []
  );

  // Keep agent nodes at fixed size — ignore accidental resize drags
  const handleNodesChange: OnNodesChange = useCallback(
    (changes) => {
      onNodesChange(changes.filter((change) => change.type !== "dimensions"));
    },
    [onNodesChange],
  );

  return (
    <div className="w-full h-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={handleNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        defaultEdgeOptions={defaultEdgeOptions}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.3}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
      >
        <Background color="#2a2d3a" gap={20} />
        <Controls
          className="!bg-surface-card !border-surface-border !rounded-lg"
          showInteractive={false}
        />
        <MiniMap
          nodeColor="#6c8cff"
          maskColor="rgba(15, 17, 23, 0.8)"
          pannable
          zoomable
        />
      </ReactFlow>

      {/* Approval Dialog */}
      {approvalAgentId && (
        <ApprovalDialog
          agentId={approvalAgentId}
          onClose={() => setApprovalAgentId(null)}
        />
      )}
    </div>
  );
}

export default OrgTree;
