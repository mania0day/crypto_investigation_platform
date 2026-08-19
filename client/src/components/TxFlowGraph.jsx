import React, { useCallback, useEffect, useMemo, useRef } from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import { computeLinearLayout } from '../utils/graphLayout';

const CYAN = '#00f0ff';
const CYAN_DIM = 'rgba(0, 240, 255, 0.35)';
const AMBER = '#f59e0b';
const CARD = '#0a1120';

const X_GAP = 210; // horizontal gap between hop levels in the linear layout
const Y_GAP = 90;
const EXPAND_R = 7; // radius of the "+" expand badge
const TWEEN_MS = 750;
const STAGGER_MS = 80; // extra delay per hop for newly revealed nodes

const nodeRadius = (node) => (node.role === 'center' ? 16 : 11);
const badgeX = (node) => node.x + nodeRadius(node) + 12;

function formatTime(ts) {
  if (!ts) return '';
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return '';
  }
}

/**
 * MetaSleuth-style interactive graph for address / tx flows.
 *
 * layout="linear": nodes are pinned into left→right hop lanes and tween
 * linearly from the node they were expanded out of to their lane position.
 * Pass onExpandNode to draw a "+" badge on unexpanded nodes; clicking it
 * requests one more hop for that node (expandedIds hides the badge once done).
 */
const TxFlowGraph = ({ graph, onNodeClick, layout = 'force', onExpandNode, expandedIds, expandingId }) => {
  const fgRef = useRef(null);
  const wrapRef = useRef(null);
  const posRef = useRef(new Map()); // id -> last rendered {x, y}
  const animRef = useRef(null);
  const sizeRef = useRef({ w: 800, h: 520 });
  const [size, setSize] = React.useState({ w: 800, h: 520 });
  const isLinear = layout === 'linear';

  const canExpand = useCallback(
    (node) => Boolean(onExpandNode) && !expandedIds?.has(node.id),
    [onExpandNode, expandedIds],
  );

  const data = useMemo(() => {
    if (!graph?.nodes?.length) {
      posRef.current = new Map();
      return { nodes: [], links: [] };
    }
    const nodes = graph.nodes.map((n) => ({ ...n }));
    const links = graph.links.map((l) => ({ ...l }));

    if (isLinear) {
      const rootId = nodes.find((n) => n.role === 'center')?.id ?? nodes[0]?.id;
      computeLinearLayout(nodes, links, rootId, { xGap: X_GAP, yGap: Y_GAP });

      const prev = posRef.current;
      const neighbors = new Map();
      const addNeighbor = (a, b) => {
        if (!neighbors.has(a)) neighbors.set(a, []);
        neighbors.get(a).push(b);
      };
      for (const l of links) {
        const s = typeof l.source === 'object' ? l.source.id : l.source;
        const t = typeof l.target === 'object' ? l.target.id : l.target;
        if (s == null || t == null) continue;
        addNeighbor(s, t);
        addNeighbor(t, s);
      }
      const rootNode = nodes.find((n) => n.id === rootId);

      for (const n of nodes) {
        // computeLinearLayout wrote the final lane position into fx/fy
        n.tx = n.fx;
        n.ty = n.fy;
        const seen = prev.get(n.id);
        if (seen) {
          n.x = seen.x;
          n.y = seen.y;
          n.spawnDelay = 0;
        } else {
          // new node: slide out of the node it was expanded from (or the root)
          const from = (neighbors.get(n.id) || []).map((id) => prev.get(id)).find(Boolean);
          const origin = from || (rootNode ? { x: rootNode.tx, y: rootNode.ty } : { x: n.tx, y: n.ty });
          n.x = origin.x;
          n.y = origin.y;
          n.spawnDelay = Math.min(320, Math.abs(Math.round(n.tx / X_GAP)) * STAGGER_MS);
        }
        n.fx = n.x;
        n.fy = n.y;
      }
      posRef.current = new Map(nodes.map((n) => [n.id, { x: n.x, y: n.y }]));
    }
    return { nodes, links };
  }, [graph, isLinear]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;

    const update = () => {
      const next = { w: el.clientWidth, h: Math.max(480, el.clientHeight) };
      sizeRef.current = next;
      setSize(next);
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (!fgRef.current || !data.nodes.length) return;
    const fg = fgRef.current;
    if (isLinear) {
      // nodes are pinned via fx/fy; silence the forces that caused the circular blob
      fg.d3Force('charge')?.strength(0);
      fg.d3Force('link')?.distance(0).strength(0);
      fg.d3Force('center', null);
    } else {
      fg.d3Force('charge')?.strength(-280);
      fg.d3Force('link')?.distance(120);
      setTimeout(() => fg.zoomToFit(400, 60), 350);
    }
  }, [data, isLinear]);

  // Linear mode: tween every node from its start position to its lane target,
  // while the camera glides to frame the final layout.
  useEffect(() => {
    if (!isLinear || !data.nodes.length) return undefined;
    cancelAnimationFrame(animRef.current);

    const nodes = data.nodes;
    const starts = new Map(nodes.map((n) => [n.id, { x: n.x, y: n.y }]));
    const maxDelay = nodes.reduce((m, n) => Math.max(m, n.spawnDelay || 0), 0);
    const moving = nodes.some((n) => n.x !== n.tx || n.y !== n.ty);

    const fg = fgRef.current;
    if (fg) {
      let minX = Infinity;
      let maxX = -Infinity;
      let minY = Infinity;
      let maxY = -Infinity;
      for (const n of nodes) {
        minX = Math.min(minX, n.tx);
        maxX = Math.max(maxX, n.tx);
        minY = Math.min(minY, n.ty);
        maxY = Math.max(maxY, n.ty);
      }
      const pad = 110;
      const { w, h } = sizeRef.current;
      const k = Math.max(0.35, Math.min(2, Math.min(w / (maxX - minX + pad * 2), h / (maxY - minY + pad * 2))));
      const camMs = moving ? TWEEN_MS + maxDelay : 0;
      fg.centerAt((minX + maxX) / 2, (minY + maxY) / 2, camMs);
      fg.zoom(k, camMs);
    }
    if (!moving) return undefined;

    const easeOut = (t) => 1 - Math.pow(1 - t, 3);
    const t0 = performance.now();
    const step = (now) => {
      let done = true;
      for (const n of nodes) {
        const s = starts.get(n.id);
        const t = Math.min(1, Math.max(0, (now - t0 - (n.spawnDelay || 0)) / TWEEN_MS));
        if (t < 1) done = false;
        const k = easeOut(t);
        n.x = s.x + (n.tx - s.x) * k;
        n.y = s.y + (n.ty - s.y) * k;
        n.fx = n.x;
        n.fy = n.y;
        posRef.current.set(n.id, { x: n.x, y: n.y });
      }
      if (!done) animRef.current = requestAnimationFrame(step);
    };
    animRef.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(animRef.current);
  }, [data, isLinear]);

  const paintNode = useCallback(
    (node, ctx, globalScale) => {
      const isCenter = node.role === 'center';
      const isTx = node.kind === 'transaction';
      const radius = nodeRadius(node);
      const label = node.label || String(node.id).slice(0, 10);

      // Glow
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius + (isCenter ? 10 : 5), 0, Math.PI * 2);
      ctx.fillStyle = isCenter ? 'rgba(0, 240, 255, 0.18)' : 'rgba(0, 240, 255, 0.06)';
      ctx.fill();

      // Card body
      ctx.beginPath();
      ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
      ctx.fillStyle = CARD;
      ctx.fill();
      ctx.lineWidth = isCenter ? 2.5 : 1.5;
      ctx.strokeStyle = isCenter ? CYAN : isTx ? AMBER : CYAN_DIM;
      ctx.stroke();

      // Inner pulse for center
      if (isCenter) {
        ctx.beginPath();
        ctx.arc(node.x, node.y, 4, 0, Math.PI * 2);
        ctx.fillStyle = CYAN;
        ctx.fill();
      }

      // "+" expand badge (MetaSleuth-style) on nodes with more hops to reveal
      if (canExpand(node)) {
        const bx = badgeX(node);
        const by = node.y;
        ctx.save();
        if (expandingId === node.id) ctx.globalAlpha = 0.35;
        ctx.beginPath();
        ctx.arc(bx, by, EXPAND_R, 0, Math.PI * 2);
        ctx.fillStyle = CARD;
        ctx.fill();
        ctx.lineWidth = 1.3;
        ctx.strokeStyle = CYAN;
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(bx - 3.4, by);
        ctx.lineTo(bx + 3.4, by);
        ctx.moveTo(bx, by - 3.4);
        ctx.lineTo(bx, by + 3.4);
        ctx.lineWidth = 1.6;
        ctx.strokeStyle = CYAN;
        ctx.stroke();
        ctx.restore();
      }

      const fontSize = Math.max(10 / globalScale, 3.2);
      ctx.font = `${isCenter ? '600' : '400'} ${fontSize}px Inter, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'top';
      ctx.fillStyle = '#e2e8f0';
      ctx.fillText(label, node.x, node.y + radius + 4);
    },
    [canExpand, expandingId],
  );

  // Clickable area = node circle + its "+" badge
  const paintPointerArea = useCallback(
    (node, color, ctx) => {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(node.x, node.y, nodeRadius(node) + 4, 0, Math.PI * 2);
      ctx.fill();
      if (canExpand(node)) {
        ctx.beginPath();
        ctx.arc(badgeX(node), node.y, EXPAND_R + 4, 0, Math.PI * 2);
        ctx.fill();
      }
    },
    [canExpand],
  );

  const handleNodeClick = useCallback(
    (node, event) => {
      if (canExpand(node) && fgRef.current && event) {
        const { x, y } = fgRef.current.screen2GraphCoords(event.offsetX, event.offsetY);
        if (Math.hypot(x - badgeX(node), y - node.y) <= EXPAND_R + 4) {
          onExpandNode(node);
          return;
        }
      }
      onNodeClick?.(node);
    },
    [canExpand, onExpandNode, onNodeClick],
  );

  const paintLink = useCallback((link, ctx, globalScale) => {
    const start = link.source;
    const end = link.target;
    if (typeof start !== 'object' || typeof end !== 'object') return;

    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const dist = Math.hypot(dx, dy) || 1;
    const ux = dx / dist;
    const uy = dy / dist;

    ctx.beginPath();
    ctx.moveTo(start.x, start.y);
    ctx.lineTo(end.x, end.y);
    ctx.strokeStyle = link.direction === 'in' ? 'rgba(34, 197, 94, 0.75)' : 'rgba(0, 240, 255, 0.7)';
    ctx.lineWidth = Math.max(1.2, 2 / globalScale);
    ctx.stroke();

    // Arrow head
    const ax = end.x - ux * 14;
    const ay = end.y - uy * 14;
    const size = 6;
    ctx.beginPath();
    ctx.moveTo(ax, ay);
    ctx.lineTo(ax - ux * size - uy * size * 0.55, ay - uy * size + ux * size * 0.55);
    ctx.lineTo(ax - ux * size + uy * size * 0.55, ay - uy * size - ux * size * 0.55);
    ctx.closePath();
    ctx.fillStyle = link.direction === 'in' ? '#22c55e' : CYAN;
    ctx.fill();

    // Edge label
    if (globalScale > 0.55 && link.amountLabel) {
      const midX = (start.x + end.x) / 2;
      const midY = (start.y + end.y) / 2 - 8;
      const fontSize = Math.max(9 / globalScale, 2.8);
      ctx.font = `500 ${fontSize}px Inter, sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      const text = `[${link.index || ''}] ${link.amountLabel}`;
      const pad = 4;
      const tw = ctx.measureText(text).width;
      ctx.fillStyle = 'rgba(5, 10, 17, 0.85)';
      ctx.fillRect(midX - tw / 2 - pad, midY - fontSize / 2 - 2, tw + pad * 2, fontSize + 6);
      ctx.fillStyle = '#f8fafc';
      ctx.fillText(text, midX, midY);

      if (link.timestamp && globalScale > 0.85) {
        const t = formatTime(link.timestamp);
        ctx.font = `400 ${Math.max(8 / globalScale, 2.4)}px Inter, sans-serif`;
        ctx.fillStyle = '#64748b';
        ctx.fillText(t, midX, midY + fontSize + 4);
      }
    }
  }, []);

  if (!data.nodes.length) {
    return (
      <div className="flex h-full min-h-[420px] items-center justify-center text-vaultrix-textMuted">
        No flow graph for this result yet.
      </div>
    );
  }

  return (
    <div ref={wrapRef} className="relative h-full min-h-[480px] w-full overflow-hidden rounded-xl border border-vaultrix-border bg-vaultrix-bg/40">
      <ForceGraph2D
        ref={fgRef}
        width={size.w}
        height={size.h}
        graphData={data}
        backgroundColor="rgba(0,0,0,0)"
        autoPauseRedraw={false}
        nodeCanvasObject={paintNode}
        nodePointerAreaPaint={paintPointerArea}
        linkCanvasObject={paintLink}
        linkDirectionalParticles={2}
        linkDirectionalParticleWidth={2}
        linkDirectionalParticleSpeed={0.006}
        linkDirectionalParticleColor={(l) => (l.direction === 'in' ? '#22c55e' : CYAN)}
        cooldownTicks={80}
        onNodeClick={handleNodeClick}
        onNodeDrag={(node) => {
          if (isLinear) {
            node.tx = node.x;
            node.ty = node.y;
          }
          posRef.current.set(node.id, { x: node.x, y: node.y });
        }}
        onNodeDragEnd={(node) => {
          node.fx = node.x;
          node.fy = node.y;
          if (isLinear) {
            node.tx = node.x;
            node.ty = node.y;
          }
          posRef.current.set(node.id, { x: node.x, y: node.y });
        }}
      />
      <div className="pointer-events-none absolute bottom-3 left-3 rounded-md border border-vaultrix-border bg-vaultrix-card/80 px-3 py-2 text-xs text-vaultrix-textMuted backdrop-blur">
        <span className="mr-3"><span className="inline-block h-2 w-2 rounded-full bg-vaultrix-cyan mr-1" /> Outflow</span>
        <span><span className="inline-block h-2 w-2 rounded-full bg-green-500 mr-1" /> Inflow</span>
        {onExpandNode && (
          <span className="ml-3 text-vaultrix-cyan">＋ expand connections</span>
        )}
      </div>
    </div>
  );
};

export default TxFlowGraph;
