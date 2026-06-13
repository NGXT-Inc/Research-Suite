import { useMemo } from 'react';
import { ReactFlow, Background, Controls, Handle, Position, MarkerType } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { layoutFigure, FIG_NODE_W } from '../utils/figureLayout';
import { useScrollLock } from './useScrollLock';

/**
 * GraphCanvasOverlay — the opt-in "view as graph" spatial view. Lazy-loaded so
 * @xyflow stays out of the first-paint budget; renders the same normalized
 * nodes/edges as GraphOutline in a 100dvh overlay with touch pan/pinch.
 * docs/MOBILE_UX_REVIEW.md §4.1. Default export → React.lazy target.
 */
function OutlineNode({ data }) {
  return (
    <div className="gcanvas-node" style={{ width: FIG_NODE_W, borderLeftColor: data.color }}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      {data.kindLabel && <div className="gcanvas-node-kind">{data.kindLabel}</div>}
      <div className="gcanvas-node-label">{data.label}</div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}
const nodeTypes = { outline: OutlineNode };

export default function GraphCanvasOverlay({ title, nodes, edges, onClose }) {
  useScrollLock(true);

  const flow = useMemo(() => {
    const laid = layoutFigure({ nodes, edges });
    const rfNodes = laid.nodes.map(n => ({
      id: n.id,
      type: 'outline',
      position: { x: n.x, y: n.y },
      data: { label: n.label, kindLabel: n.kindLabel, color: n.color },
      draggable: false,
      connectable: false,
    }));
    const rfEdges = (laid.edges || []).map((e, i) => ({
      id: `${e.from}->${e.to}:${i}`,
      source: e.from,
      target: e.to,
      type: 'smoothstep',
      label: e.label || undefined,
      markerEnd: { type: MarkerType.ArrowClosed, width: 12, height: 12 },
    }));
    return { rfNodes, rfEdges };
  }, [nodes, edges]);

  return (
    <div className="gcanvas-overlay" role="dialog" aria-modal="true" aria-label={title}>
      <div className="gcanvas-bar">
        <span className="gcanvas-title">{title}</span>
        <button type="button" className="btn btn--sm" onClick={onClose}>✕ Close</button>
      </div>
      <div className="gcanvas-flow">
        <ReactFlow
          nodes={flow.rfNodes}
          edges={flow.rfEdges}
          nodeTypes={nodeTypes}
          fitView
          proOptions={{ hideAttribution: true }}
          nodesDraggable={false}
          nodesConnectable={false}
          edgesFocusable={false}
          zoomOnDoubleClick={false}
          zoomOnPinch
          panOnDrag
          minZoom={0.2}
          maxZoom={2}
        >
          <Background gap={22} size={1} />
          <Controls showInteractive={false} position="bottom-right" />
        </ReactFlow>
      </div>
    </div>
  );
}
