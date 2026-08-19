/**
 * Assigns each node a fixed position for a LINEAR left→right flow layout,
 * instead of a force simulation's circular blob.
 *
 * x = signed hop distance from the root along edge direction:
 *   downstream recipients (root → … → node) go to the right (+),
 *   upstream senders (node → … → root) go to the left (−).
 * Nodes sharing a level are stacked vertically and centered.
 *
 * Mutates and returns the passed nodes with { fx, fy } set (d3-force pins them).
 */
export function computeLinearLayout(nodes, links, rootId, { xGap = 210, yGap = 90 } = {}) {
  if (!nodes?.length) return nodes;

  const idOf = (endpoint) => (typeof endpoint === 'object' && endpoint ? endpoint.id : endpoint);
  const adj = new Map();
  const addNeighbor = (from, to, sign) => {
    if (!adj.has(from)) adj.set(from, []);
    adj.get(from).push({ id: to, sign });
  };
  for (const l of links || []) {
    const s = idOf(l.source);
    const t = idOf(l.target);
    if (s == null || t == null) continue;
    addNeighbor(s, t, +1); // s sends to t → t is one level downstream
    addNeighbor(t, s, -1); // from t's view, s is one level upstream
  }

  const start = nodes.some((n) => n.id === rootId) ? rootId : nodes[0].id;
  const level = new Map([[start, 0]]);
  const queue = [start];
  while (queue.length) {
    const cur = queue.shift();
    const curLevel = level.get(cur);
    for (const { id, sign } of adj.get(cur) || []) {
      if (!level.has(id)) {
        level.set(id, curLevel + sign);
        queue.push(id);
      }
    }
  }

  // group nodes by level (disconnected nodes fall to level 0)
  const byLevel = new Map();
  for (const n of nodes) {
    const lv = level.has(n.id) ? level.get(n.id) : 0;
    if (!byLevel.has(lv)) byLevel.set(lv, []);
    byLevel.get(lv).push(n);
  }
  for (const [lv, group] of byLevel) {
    group.sort((a, b) => String(a.id).localeCompare(String(b.id)));
    const mid = (group.length - 1) / 2;
    group.forEach((n, i) => {
      n.fx = lv * xGap;
      n.fy = (i - mid) * yGap;
    });
  }
  return nodes;
}
