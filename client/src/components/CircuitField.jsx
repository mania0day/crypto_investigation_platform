import React, { useEffect, useRef } from 'react';

const MAX_DPR = 1.5;
const HEX_STEP_X = 128;
const HEX_STEP_Y = 112;
const PARALLAX = 14;

/** Deterministic PRNG so a given viewport always lays out the same board. */
function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function hexPoints(cx, cy, r) {
  const pts = [];
  for (let i = 0; i < 6; i += 1) {
    const a = (Math.PI / 3) * i + Math.PI / 6;
    pts.push({ x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r });
  }
  return pts;
}

function tracePolyline(ctx, pts) {
  ctx.beginPath();
  ctx.moveTo(pts[0].x, pts[0].y);
  for (let i = 1; i < pts.length; i += 1) ctx.lineTo(pts[i].x, pts[i].y);
}

/** PCB-style route: axis-aligned runs joined by a single 45° jog. */
function routeManhattan45(x1, y1, x2, y2, rng) {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const sx = Math.sign(dx);
  const sy = Math.sign(dy);
  const diag = Math.min(Math.abs(dx), Math.abs(dy));
  const pts = [{ x: x1, y: y1 }];

  if (rng() < 0.55) {
    const straight = (Math.abs(dx) - diag) * (0.25 + rng() * 0.55);
    pts.push({ x: x1 + sx * straight, y: y1 });
    pts.push({ x: x1 + sx * (straight + diag), y: y1 + sy * diag });
  } else {
    pts.push({ x: x1 + sx * diag, y: y1 + sy * diag });
  }
  pts.push({ x: x2, y: y2 });

  return pts.filter(
    (p, i, arr) => i === 0 || Math.hypot(p.x - arr[i - 1].x, p.y - arr[i - 1].y) > 1
  );
}

function measurePath(pts) {
  const segs = [];
  let total = 0;
  for (let i = 1; i < pts.length; i += 1) {
    const a = pts[i - 1];
    const b = pts[i];
    const len = Math.hypot(b.x - a.x, b.y - a.y);
    if (len < 0.5) continue;
    segs.push({ a, b, len, start: total, angle: Math.atan2(b.y - a.y, b.x - a.x) });
    total += len;
  }
  return { pts, segs, total };
}

function pointAt(path, dist) {
  if (!path.total) return null;
  let d = dist % path.total;
  if (d < 0) d += path.total;
  for (let i = 0; i < path.segs.length; i += 1) {
    const s = path.segs[i];
    if (d <= s.start + s.len) {
      const t = (d - s.start) / s.len;
      return {
        x: s.a.x + (s.b.x - s.a.x) * t,
        y: s.a.y + (s.b.y - s.a.y) * t,
        angle: s.angle
      };
    }
  }
  const last = path.segs[path.segs.length - 1];
  return { x: last.b.x, y: last.b.y, angle: last.angle };
}

function buildBoard(w, h) {
  const rng = mulberry32(Math.round(w) * 73856093 ^ Math.round(h) * 19349663);
  const hexes = [];

  const cols = Math.ceil(w / HEX_STEP_X) + 2;
  const rows = Math.ceil(h / HEX_STEP_Y) + 2;

  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < cols; col += 1) {
      const x = col * HEX_STEP_X - HEX_STEP_X * 0.6 + (row % 2 ? HEX_STEP_X * 0.5 : 0);
      const y = row * HEX_STEP_Y - HEX_STEP_Y * 0.5;

      // Board density grows toward the lower-right, leaving the upper-left calm.
      const density = Math.min(0.72, (x / w) * 0.8 + (y / h) * 0.5 - 0.3);
      if (rng() > density) continue;

      hexes.push({
        x,
        y,
        r: 16 + rng() * 34,
        filled: rng() < 0.5,
        phase: rng() * Math.PI * 2,
        speed: 0.4 + rng() * 0.7
      });
    }
  }

  hexes.sort((a, b) => a.x - b.x);

  const traces = [];
  const pushTrace = (pts, opts = {}) => {
    const path = measurePath(pts);
    if (path.total < 60) return;
    traces.push({
      path,
      pads: opts.pads !== false,
      chevrons: rng() < 0.32,
      chevronDir: rng() < 0.22 ? -1 : 1,
      pulses: Array.from({ length: rng() < 0.35 ? 2 : 1 }, () => ({
        offset: rng() * path.total,
        speed: 52 + rng() * 78
      }))
    });
  };

  hexes.forEach((hex, i) => {
    for (let back = 1; back <= 3 && i - back >= 0; back += 1) {
      const other = hexes[i - back];
      const dist = Math.hypot(hex.x - other.x, hex.y - other.y);
      if (dist < 70 || dist > 340) continue;
      if (rng() > 0.45) continue;
      pushTrace(routeManhattan45(other.x, other.y, hex.x, hex.y, rng));
      break;
    }
  });

  // Feeder traces entering from the left/top edges toward the dense cluster.
  const feeders = Math.round(6 + (w / 1600) * 6);
  for (let i = 0; i < feeders; i += 1) {
    const target = hexes[Math.floor(rng() * hexes.length)];
    if (!target) break;
    const fromLeft = rng() < 0.7;
    const sx = fromLeft ? -20 : rng() * w * 0.5;
    const sy = fromLeft ? h * (0.18 + rng() * 0.72) : -20;
    pushTrace(routeManhattan45(sx, sy, target.x, target.y, rng));
  }

  return { hexes, traces };
}

/** Static geometry is rasterized once per resize; only pulses redraw each frame. */
function paintStatic(ctx, board, w, h) {
  ctx.clearRect(0, 0, w, h);

  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';

  board.traces.forEach(({ path, pads }) => {
    ctx.strokeStyle = 'rgba(64, 168, 255, 0.22)';
    ctx.lineWidth = 1;
    tracePolyline(ctx, path.pts);
    ctx.stroke();

    if (!pads) return;
    const head = path.pts[0];
    ctx.beginPath();
    ctx.arc(head.x, head.y, 3.2, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(6, 16, 32, 0.9)';
    ctx.fill();
    ctx.strokeStyle = 'rgba(90, 200, 255, 0.5)';
    ctx.lineWidth = 1.2;
    ctx.stroke();
  });

  board.hexes.forEach((hex) => {
    const pts = hexPoints(hex.x, hex.y, hex.r);
    tracePolyline(ctx, pts);
    ctx.closePath();

    if (hex.filled) {
      ctx.fillStyle = 'rgba(14, 62, 130, 0.34)';
      ctx.fill();
    }
    ctx.strokeStyle = 'rgba(70, 176, 255, 0.3)';
    ctx.lineWidth = 1;
    ctx.stroke();

    if (hex.r > 34) {
      tracePolyline(ctx, hexPoints(hex.x, hex.y, hex.r - 7));
      ctx.closePath();
      ctx.strokeStyle = 'rgba(70, 176, 255, 0.14)';
      ctx.stroke();
    }
  });
}

function paintDynamic(ctx, board, t) {
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';

  board.hexes.forEach((hex) => {
    const breathe = 0.5 + 0.5 * Math.sin(t * hex.speed + hex.phase);
    if (breathe < 0.08) return;

    tracePolyline(ctx, hexPoints(hex.x, hex.y, hex.r));
    ctx.closePath();
    ctx.strokeStyle = `rgba(120, 226, 255, ${0.1 + breathe * 0.34})`;
    ctx.lineWidth = 1.3;
    ctx.stroke();

    const glow = ctx.createRadialGradient(hex.x, hex.y, 0, hex.x, hex.y, hex.r * 1.7);
    glow.addColorStop(0, `rgba(0, 190, 255, ${0.05 + breathe * 0.1})`);
    glow.addColorStop(1, 'rgba(0, 190, 255, 0)');
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(hex.x, hex.y, hex.r * 1.7, 0, Math.PI * 2);
    ctx.fill();
  });

  board.traces.forEach((trace) => {
    const { path } = trace;

    if (trace.chevrons) {
      const spacing = 34;
      const march = ((t * 26 * trace.chevronDir) % spacing + spacing) % spacing;
      for (let d = march; d < path.total; d += spacing) {
        const p = pointAt(path, d);
        if (!p) continue;
        const fade = Math.min(1, d / 60) * Math.min(1, (path.total - d) / 60);
        if (fade <= 0.02) continue;
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(p.angle + (trace.chevronDir < 0 ? Math.PI : 0));
        ctx.beginPath();
        ctx.moveTo(-4, -3.4);
        ctx.lineTo(1.6, 0);
        ctx.lineTo(-4, 3.4);
        ctx.strokeStyle = `rgba(110, 224, 255, ${0.3 * fade})`;
        ctx.lineWidth = 1.2;
        ctx.stroke();
        ctx.restore();
      }
    }

    trace.pulses.forEach((pulse) => {
      const head = pulse.offset + t * pulse.speed;
      const tailSteps = 9;

      for (let i = tailSteps; i >= 0; i -= 1) {
        const p = pointAt(path, head - i * 5.5);
        if (!p) continue;
        const k = 1 - i / (tailSteps + 1);
        ctx.beginPath();
        ctx.arc(p.x, p.y, 0.7 + k * 1.5, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(150, 240, 255, ${0.05 + k * k * 0.65})`;
        ctx.fill();
      }

      const tip = pointAt(path, head);
      if (!tip) return;
      const halo = ctx.createRadialGradient(tip.x, tip.y, 0, tip.x, tip.y, 13);
      halo.addColorStop(0, 'rgba(160, 245, 255, 0.5)');
      halo.addColorStop(1, 'rgba(0, 200, 255, 0)');
      ctx.fillStyle = halo;
      ctx.beginPath();
      ctx.arc(tip.x, tip.y, 13, 0, Math.PI * 2);
      ctx.fill();
    });
  });
}

/**
 * Procedural circuit board: PCB traces carrying data pulses between
 * breathing hex nodes. Replaces the previous panning GIF background.
 */
const CircuitField = () => {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;

    const ctx = canvas.getContext('2d');
    const staticLayer = document.createElement('canvas');
    const staticCtx = staticLayer.getContext('2d');

    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    let board = null;
    let width = 0;
    let height = 0;
    let dpr = 1;
    let raf = 0;
    let resizeTimer = 0;
    let start = 0;

    const pointer = { x: 0, y: 0, cx: 0, cy: 0 };

    const layout = () => {
      const rect = canvas.getBoundingClientRect();
      width = Math.max(320, Math.round(rect.width));
      height = Math.max(320, Math.round(rect.height));
      dpr = Math.min(MAX_DPR, window.devicePixelRatio || 1);

      [canvas, staticLayer].forEach((c) => {
        c.width = Math.round(width * dpr);
        c.height = Math.round(height * dpr);
      });
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      staticCtx.setTransform(dpr, 0, 0, dpr, 0, 0);

      board = buildBoard(width, height);
      paintStatic(staticCtx, board, width, height);
    };

    const frame = (now) => {
      if (!start) start = now;
      const t = (now - start) / 1000;

      pointer.cx += (pointer.x - pointer.cx) * 0.045;
      pointer.cy += (pointer.y - pointer.cy) * 0.045;

      ctx.setTransform(dpr, 0, 0, dpr, pointer.cx * dpr, pointer.cy * dpr);
      ctx.clearRect(-PARALLAX, -PARALLAX, width + PARALLAX * 2, height + PARALLAX * 2);

      ctx.drawImage(staticLayer, 0, 0, width, height);
      paintDynamic(ctx, board, t);

      raf = window.requestAnimationFrame(frame);
    };

    const stop = () => {
      if (raf) window.cancelAnimationFrame(raf);
      raf = 0;
    };

    const run = () => {
      if (raf || reduceMotion) return;
      start = 0;
      raf = window.requestAnimationFrame(frame);
    };

    const onResize = () => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        layout();
        if (reduceMotion) {
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
          ctx.clearRect(0, 0, width, height);
          ctx.drawImage(staticLayer, 0, 0, width, height);
        }
      }, 180);
    };

    const onPointerMove = (e) => {
      pointer.x = ((e.clientX / window.innerWidth) * 2 - 1) * -PARALLAX;
      pointer.y = ((e.clientY / window.innerHeight) * 2 - 1) * -PARALLAX;
    };

    const onVisibility = () => (document.hidden ? stop() : run());

    layout();
    if (reduceMotion) {
      ctx.drawImage(staticLayer, 0, 0, width, height);
    } else {
      run();
      window.addEventListener('pointermove', onPointerMove, { passive: true });
      document.addEventListener('visibilitychange', onVisibility);
    }
    window.addEventListener('resize', onResize);

    return () => {
      stop();
      window.clearTimeout(resizeTimer);
      window.removeEventListener('resize', onResize);
      window.removeEventListener('pointermove', onPointerMove);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  return <canvas ref={canvasRef} className="cipher-circuit-canvas" aria-hidden="true" />;
};

export default CircuitField;
