import { useEffect, useMemo } from "react";
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
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import AgentNode from "./AgentNode";
import { getOrgTree } from "../api";

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
  parentId?: string
): void {
  // Add current node
  nodes.push({
    id: node.id,
    type: "agent",
    position: { x, y },
    data: {
      name: node.name,
      role: node.role,
      status: node.status,
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
      layoutTree(child, childCenterX, childY, nodes, edges, node.id);
      childX += childSubtreeWidth + (X_SPACING - NODE_WIDTH);
    }
  }
}

function OrgTree() {
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);

  // Fetch and layout org tree
  useEffect(() => {
    async function fetchTree() {
      const layoutRoots = (roots: OrgNodeData[]) => {
        const newNodes: Node[] = [];
        const newEdges: Edge[] = [];

        let offsetX = 0;
        for (const root of roots) {
          const w = getSubtreeWidth(root);
          layoutTree(root, offsetX + w / 2 - NODE_WIDTH / 2, 0, newNodes, newEdges);
          offsetX += w + X_SPACING;
        }

        setNodes(newNodes);
        setEdges(newEdges);
      };

      try {
        const data = await getOrgTree();

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

        if (roots.length === 0) {
          console.warn("Unexpected org tree shape:", data);
          throw new Error("No valid root nodes in tree data");
        }
        layoutRoots(roots);
      } catch (err) {
        console.error("Failed to fetch org tree:", err);
        // Fallback demo data
        const demoTree: OrgNodeData = {
          id: "architect-1",
          name: "System Architect",
          role: "architect",
          status: "idle",
          children: [
            {
              id: "manager-1",
              name: "Project Manager",
              role: "manager",
              status: "working",
              children: [
                { id: "dev-1", name: "Backend Dev", role: "module_dev", status: "idle" },
                { id: "dev-2", name: "Frontend Dev", role: "module_dev", status: "working" },
              ],
            },
            { id: "qa-1", name: "QA Engineer", role: "qa", status: "idle" },
          ],
        };
        layoutRoots([demoTree]);
      }
    }

    fetchTree();
  }, [setNodes, setEdges]);

  const defaultEdgeOptions = useMemo(
    () => ({
      type: "smoothstep",
      style: { stroke: "#2a2d3a", strokeWidth: 2 },
    }),
    []
  );

  return (
    <div className="w-full h-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
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
    </div>
  );
}

export default OrgTree;
