import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import './fundFlowHero.css';

/* Fund-flow trace hero — ported from the "Chainsight Dashboard Hero" design.
   A 1600×1000 stage scaled to fit its container. Intro sequence:
   1) description rises in, 2) module row sweeps in horizontally,
   3) the trace animation plays — wallet cards reveal as transactions land,
   particles travel the edges, then a live feed replays. */

const STAGE_W = 1600;
const STAGE_H = 1080;
const CARD_W = 196;
const CARD_H = 74;
const TRAVEL = 0.65;

const TONE = { safe: '#34D399', warn: '#FBBF24', alert: '#F87171', neutral: '#38BDF8' };

const MONO = "'JetBrains Mono', monospace";

// Layered wallet graph: col 0 = sources, col 1 = intermediaries (around the core), col 2 = destinations.
const NODES = [
  { id: 'w1',  addr: '0x8f2c…19cd', role: 'Exchange Deposit',    volume: '12.40 ETH OUT', tone: 'safe',    risk: 12, x: 138,  y: 360 },
  { id: 'w2',  addr: '0x41ab…77e2', role: 'Unattributed',        volume: '9.90 ETH OUT',  tone: 'neutral', risk: 34, x: 138,  y: 510 },
  { id: 'w3',  addr: '0x5d70…33c9', role: 'DeFi Pool Exit',      volume: '7.40 ETH OUT',  tone: 'neutral', risk: 41, x: 138,  y: 660 },
  { id: 'w4',  addr: '0x1e44…8b6f', role: 'Cold Storage',        volume: '6.60 ETH OUT',  tone: 'safe',    risk: 9,  x: 138,  y: 810 },
  { id: 'w5',  addr: '0xd93f…0a14', role: 'Mixer Entry',         volume: '8.10 ETH THRU', tone: 'warn',    risk: 71, x: 650,  y: 330 },
  { id: 'w6',  addr: '0xbb31…7d05', role: 'Bridge Contract',     volume: '5.00 ETH THRU', tone: 'neutral', risk: 38, x: 650,  y: 470 },
  { id: 'w7',  addr: '0x9a10…4f3b', role: 'OTC Desk',            volume: '2.30 ETH THRU', tone: 'warn',    risk: 64, x: 650,  y: 770 },
  { id: 'w8',  addr: '0x63f1…d47c', role: 'Sanctioned Entity',   volume: '4.20 ETH THRU', tone: 'alert',   risk: 96, x: 650,  y: 890 },
  { id: 'w9',  addr: '0x0b52…91aa', role: 'Exchange Withdrawal', volume: '6.10 ETH IN',   tone: 'safe',    risk: 14, x: 1160, y: 380 },
  { id: 'w10', addr: '0x77de…c208', role: 'Unattributed',        volume: '3.40 ETH IN',   tone: 'neutral', risk: 33, x: 1160, y: 530 },
  { id: 'w11', addr: '0xac89…2e10', role: 'Unattributed',        volume: '4.90 ETH IN',   tone: 'neutral', risk: 29, x: 1160, y: 680 },
  { id: 'w12', addr: '0x2c67…be55', role: 'Miner Payout',        volume: '2.80 ETH IN',   tone: 'neutral', risk: 26, x: 1160, y: 830 },
];

// Transactions (edges). Order = the sequence the live feed replays.
const TXS = [
  { from: 'w1', to: 'w5',  value: '12.40 ETH', hash: '0x9c…41' },
  { from: 'w1', to: 'w6',  value: '3.10 ETH',  hash: '0x4a…b2' },
  { from: 'w2', to: 'w6',  value: '9.90 ETH',  hash: '0x77…de' },
  { from: 'w2', to: 'w7',  value: '2.05 ETH',  hash: '0x18…9f' },
  { from: 'w3', to: 'w7',  value: '7.40 ETH',  hash: '0xc2…07' },
  { from: 'w3', to: 'w8',  value: '1.60 ETH',  hash: '0x5e…33' },
  { from: 'w4', to: 'w8',  value: '6.60 ETH',  hash: '0xaa…16' },
  { from: 'w4', to: 'w5',  value: '2.20 ETH',  hash: '0x30…c8' },
  { from: 'w5', to: 'w9',  value: '6.10 ETH',  hash: '0xf1…52' },
  { from: 'w5', to: 'w10', value: '2.90 ETH',  hash: '0x6b…a9' },
  { from: 'w6', to: 'w10', value: '3.40 ETH',  hash: '0x2d…70' },
  { from: 'w6', to: 'w11', value: '4.90 ETH',  hash: '0x8e…04' },
  { from: 'w7', to: 'w11', value: '1.80 ETH',  hash: '0x1b…e5' },
  { from: 'w7', to: 'w12', value: '2.80 ETH',  hash: '0xd4…9a' },
  { from: 'w8', to: 'w12', value: '0.95 ETH',  hash: '0x7f…3c' },
];

const STATS = [
  { target: 48219304, label: 'ADDRESSES INDEXED' },
  { target: 1284,     label: 'ACTIVE TRACES' },
  { target: 21940117, label: 'BLOCKS SYNCED' },
];

const MODULES = [
  { num: '01', title: 'Fund-Flow Trace' },
  { num: '02', title: 'Risk Scoring Engine' },
  { num: '03', title: 'Wallet Clustering' },
  { num: '04', title: 'Cross-Chain Bridge Trace' },
  { num: '05', title: 'Sanctions Screening', alert: true },
  { num: '06', title: 'AI Investigation Assistant' },
];

const PIPELINE = [
  { color: '#38BDF8', text: 'Ingesting new blocks…',          meta: 'RUNNING · ETH, ARB, BASE', blip: '1.4s' },
  { color: '#FBBF24', text: 'Re-screening sanctions lists…',  meta: 'IN REVIEW · 1 MATCH',      blip: '1.9s' },
  { color: '#34D399', text: 'Trace verified',                 meta: 'COMPLETE · 15 EDGES',      blip: null },
];

const LEGEND = [
  { color: '#38BDF8', label: 'UNATTRIBUTED WALLET' },
  { color: '#34D399', label: 'ATTRIBUTED / LOW RISK' },
  { color: '#FBBF24', label: 'MEDIUM RISK' },
  { color: '#F87171', label: 'SANCTIONED / HIGH RISK' },
];

// Ambient drifting motes behind the graph: [left%, top%, size px, dur s, delay s]
const FLOAT_DOTS = [
  [6, 38, 3, 9, 0], [12, 72, 2, 11, 2.2], [21, 30, 2.5, 8, 1.1], [28, 84, 2, 12, 0.6],
  [37, 46, 3, 10, 3.1], [44, 25, 2, 9.5, 1.8], [52, 78, 2.5, 11.5, 0.3], [58, 38, 2, 8.5, 2.7],
  [66, 66, 3, 10.5, 1.4], [72, 30, 2, 12.5, 3.6], [79, 74, 2.5, 9, 0.9], [85, 44, 2, 11, 2],
  [91, 62, 3, 10, 1.6], [96, 33, 2, 12, 0.2],
];

// HUD frame corners: position + which two borders draw the L-bracket
const HUD_CORNERS = [
  { left: 18, top: 18, borderWidth: '1px 0 0 1px' },
  { right: 18, top: 18, borderWidth: '1px 1px 0 0' },
  { left: 18, bottom: 18, borderWidth: '0 0 1px 1px' },
  { right: 18, bottom: 18, borderWidth: '0 1px 1px 0' },
];

// Equalizer bars: [duration s, delay s]
const BARS = [
  [1.1, 0], [1.4, 0.1], [0.9, 0.2], [1.6, 0.05], [1.2, 0.3], [1, 0.15],
  [1.5, 0.25], [1.3, 0.35], [1.05, 0.45], [1.45, 0.2], [1.15, 0.4], [1.35, 0.12],
];

const OPACITY_LADDER = [1, 0.68, 0.44, 0.24];

// Intro pacing: description → module row → graph.
const MODULE_DELAY = 0.38;
const MODULE_STEP = 0.07;
const PANEL_DELAY = 0.95;
const GRAPH_LEAD = 1.2;

const fmt = (n) => Math.round(n).toLocaleString('en-US');
const smooth = (p) => p * p * (3 - 2 * p);
const bell = (p) => Math.sin(Math.PI * Math.max(0, Math.min(1, p)));
const clamp01 = (p) => Math.max(0, Math.min(1, p));
// snappy pop with a slight overshoot past 1 before settling
const backOut = (p) => { const c1 = 1.70158, c3 = c1 + 1, q = p - 1; return 1 + c3 * q * q * q + c1 * q * q; };
const outCubic = (p) => 1 - Math.pow(1 - p, 3);
const hexGlow = (hex) => {
  const h = hex.replace('#', '');
  return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},`;
};

function buildGraph() {
  const nodes = NODES.map((n) => {
    const color = TONE[n.tone] || TONE.neutral;
    const glow = hexGlow(color);
    const frame = glow + '0.35)';
    return {
      ...n,
      color,
      glow,
      // resting look: each wallet is tinted by its risk tone so types read apart at a glance
      bgRest: `linear-gradient(180deg, ${glow}0.16) 0%, rgba(10,15,23,0.97) 60%)`,
      frameRest: `${frame} ${frame} ${frame} ${color}`,
      left: n.x - CARD_W / 2,
      top: n.y - CARD_H / 2,
      revealAt: Infinity,
    };
  });
  const byId = {};
  nodes.forEach((n) => { byId[n.id] = n; });

  const STEP = 0.45;
  const edges = TXS.map((t, i) => {
    const a = byId[t.from];
    const b = byId[t.to];
    if (!a || !b) return null;
    // each transaction: sender appears → connector draws → particle travels → receiver appears
    const at = GRAPH_LEAD + i * STEP;
    const drawAt = at + 0.2;
    const travelAt = at + 0.5;
    const landAt = travelAt + TRAVEL * 0.82;
    a.revealAt = Math.min(a.revealAt, at);
    b.revealAt = Math.min(b.revealAt, landAt);
    const x1 = a.x + CARD_W / 2 + 2;
    const y1 = a.y;
    const x2 = b.x - CARD_W / 2 - 8;
    const y2 = b.y;
    const dx = (x2 - x1) * 0.55;
    return {
      id: 'e' + i,
      value: t.value,
      hash: t.hash,
      from: t.from,
      to: t.to,
      fromAddr: a.addr,
      toAddr: b.addr,
      d: `M ${x1} ${y1} C ${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`,
      at,
      drawAt,
      travelAt,
      landAt,
      mx: Math.round((x1 + x2) / 2),
      my: Math.round((y1 + y2) / 2 - 14),
      ex2: x2,
      ey2: y2,
    };
  }).filter(Boolean);

  return { nodes, edges };
}

export default function FundFlowHero({ txIntervalMs = 700, onWalletIntelligence }) {
  const outerRef = useRef(null);
  const elsRef = useRef({});
  const logRef = useRef([]);
  const seqRef = useRef(0);
  const [scale, setScale] = useState(0);
  const [log, setLog] = useState([]);
  const [activeModule, setActiveModule] = useState(0);

  const graph = useMemo(buildGraph, []);

  // Cycle the highlighted module, echoing a scanner stepping through the rail.
  useEffect(() => {
    const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) return undefined;
    const id = setInterval(() => setActiveModule((m) => (m + 1) % MODULES.length), 2200);
    return () => clearInterval(id);
  }, []);

  // Scale the fixed 1600×1000 stage to the container width. ResizeObserver
  // alone is not enough: its callbacks ride the render loop, which stalls in
  // hidden/background documents — so retry until layout reports a width and
  // also listen to window resize.
  useLayoutEffect(() => {
    const el = outerRef.current;
    if (!el) return undefined;
    let cancelled = false;
    let timer;
    const measure = () => {
      if (cancelled) return;
      const w = el.clientWidth;
      if (w > 0) setScale(Math.min(w / STAGE_W, 1.25));
      else timer = setTimeout(measure, 200);
    };
    measure();
    const onResize = () => {
      const w = el.clientWidth;
      if (w > 0) setScale(Math.min(w / STAGE_W, 1.25));
    };
    const ro = new ResizeObserver(onResize);
    ro.observe(el);
    window.addEventListener('resize', onResize);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      ro.disconnect();
      window.removeEventListener('resize', onResize);
    };
  }, []);

  useLayoutEffect(() => {
    const els = elsRef.current;
    const { nodes, edges } = graph;

    const row = (e, i) => ({
      key: e.id + '-' + seqRef.current++,
      pair: e.fromAddr + '  →  ' + e.toAddr,
      meta: e.value + ' · tx ' + e.hash,
      opacity: OPACITY_LADDER[i] ?? 0.2,
    });

    const pushLog = (e) => {
      logRef.current = [row(e, 0)].concat(
        logRef.current.slice(0, 3).map((r, i) => ({ ...r, opacity: OPACITY_LADDER[i + 1] ?? 0.2 }))
      );
      setLog(logRef.current);
    };

    const reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) {
      STATS.forEach((s, i) => { const el = els['stat' + i]; if (el) el.textContent = fmt(s.target); });
      nodes.forEach((n) => {
        const c = els['card-' + n.id];
        if (c) { c.style.opacity = '1'; c.style.transform = 'none'; }
      });
      edges.forEach((e) => {
        const l = els['line-' + e.id];
        if (l) { l.style.opacity = '1'; l.style.strokeDasharray = 'none'; }
      });
      logRef.current = edges.slice(-4).reverse().map((e, i) => row(e, i));
      setLog(logRef.current);
      if (els.eqcap) {
        els.eqcap.textContent = 'TRACE COMPLETE · MONITORING LIVE TRANSFERS';
        els.eqcap.style.color = '#34D399';
      }
      return undefined;
    }

    const st = { t0: 0, lastFire: -999, cursor: 0, active: [], logged: {}, len: {}, settleAt: 0 };
    const interval = txIntervalMs / 1000;
    const dur = 1.5;

    const lenOf = (e) => {
      if (st.len[e.id] == null) {
        const l = els['line-' + e.id];
        st.len[e.id] = l && l.getTotalLength ? l.getTotalLength() : 0;
      }
      return st.len[e.id];
    };

    const fire = (t) => {
      const e = edges[st.cursor % edges.length];
      st.cursor++;
      st.active.push({ e, t0: t });
      pushLog(e);
    };

    let raf;
    const tick = (now) => {
      const t = now / 1000;
      if (!st.t0) st.t0 = t;
      const el = t - st.t0;

      if (!st.settleAt) st.settleAt = Math.max.apply(null, edges.map((e) => e.travelAt)) + TRAVEL + 0.8;
      if (el > st.settleAt && t - st.lastFire > interval) { st.lastFire = t; fire(t); }
      st.active = st.active.filter((a) => t - a.t0 < dur);

      // headline decodes into place on boot
      if (!st.headDone && els.headline) {
        const full = 'REAL-TIME FUND-FLOW INTELLIGENCE';
        const q = clamp01((el - 0.05) / 0.85);
        if (q >= 1) { st.headDone = 1; els.headline.textContent = full; }
        else {
          const GLYPHS = '01<>[]#$%&*+=/';
          const n = Math.floor(q * full.length);
          let out = full.slice(0, n);
          for (let i = n; i < full.length; i++) {
            const ch = full[i];
            out += (ch === ' ' || ch === '-') ? ch : GLYPHS[(Math.random() * GLYPHS.length) | 0];
          }
          els.headline.textContent = out;
        }
      }

      // once the full trace has settled, the status line flips to its verdict
      if (!st.capDone && el > st.settleAt && els.eqcap) {
        st.capDone = 1;
        els.eqcap.textContent = 'TRACE COMPLETE · MONITORING LIVE TRANSFERS';
        els.eqcap.style.color = '#34D399';
      }

      // reveal each wallet card at the moment its first transaction touches it
      // (opacity here; transform is composed with the pulse below)
      nodes.forEach((n) => {
        const card = els['card-' + n.id];
        if (!card) return;
        const k = clamp01((el - n.revealAt) / 0.38);
        if (card._k === k) return;
        card._k = k;
        card.style.opacity = smooth(k).toFixed(3);
      });

      const hot = {};
      edges.forEach((e) => {
        const pp = (el - e.travelAt) / TRAVEL;
        if (pp >= 0 && pp <= 1) hot[e.id] = pp;
        if (pp >= 0 && !st.logged[e.id]) { st.logged[e.id] = 1; pushLog(e); }
      });
      st.active.forEach((a) => { if (hot[a.e.id] == null) hot[a.e.id] = (t - a.t0) / 0.9; });

      let coreAct = 0;
      edges.forEach((e) => {
        const line = els['line-' + e.id];
        const trail = els['t-' + e.id];
        const dot = els['p-' + e.id];
        const glow = els['g-' + e.id];
        const label = els['l-' + e.id];
        const len = lenOf(e);

        // draw-on: each connector traces itself once both of its wallets have landed
        const draw = clamp01((el - e.drawAt) / 0.28);
        if (line) {
          line.style.opacity = draw > 0 ? '1' : '0';
          if (draw < 1) {
            line.style.strokeDasharray = len + 'px';
            line.style.strokeDashoffset = (len * (1 - outCubic(draw))) + 'px';
            line.setAttribute('marker-end', 'none');
          } else if (line.style.strokeDasharray !== 'none') {
            line.style.strokeDasharray = 'none';
            line.style.strokeDashoffset = '0';
            line.setAttribute('marker-end', 'url(#om-arrow)');
          }
        }

        // landing ripple: a ring expands where the transfer hits the receiving wallet
        const rip = els['r-' + e.id];
        if (rip) {
          let rs = el - (e.travelAt + TRAVEL * 0.8);
          st.active.forEach((a) => {
            if (a.e.id !== e.id) return;
            const s2 = t - (a.t0 + 0.72);
            if (s2 >= 0 && (rs < 0 || s2 < rs)) rs = s2;
          });
          if (draw >= 1 && rs >= 0 && rs <= 0.55) {
            const rk = rs / 0.55;
            rip.setAttribute('r', (6 + 30 * outCubic(rk)).toFixed(1));
            rip.setAttribute('opacity', (0.55 * (1 - rk)).toFixed(3));
          } else if (rip.getAttribute('opacity') !== '0') {
            rip.setAttribute('opacity', '0');
          }
        }

        const p = hot[e.id];
        if (p == null || draw < 1) {
          if (line && draw >= 1) { line.style.strokeWidth = '1.2'; line.style.filter = 'none'; }
          if (trail) trail.setAttribute('opacity', '0');
          if (dot) dot.setAttribute('opacity', '0');
          if (glow) glow.setAttribute('opacity', '0');
          if (label) label.style.opacity = '0';
          return;
        }
        if (!line) return;

        const travel = smooth(Math.min(1, p / 0.8));
        const head = len * travel;
        const pt = line.getPointAtLength(head);
        const fade = p < 0.12 ? p / 0.12 : (p > 0.86 ? (1 - p) / 0.14 : 1);
        line.style.strokeWidth = (1.2 + 1.1 * bell(p)).toFixed(2);
        line.setAttribute('marker-end', 'url(#om-arrow-hot)');
        // comet tail riding the curve just behind the particle
        if (trail) {
          trail.setAttribute('stroke-dasharray', '46 ' + len);
          trail.setAttribute('stroke-dashoffset', String(46 - head));
          trail.setAttribute('opacity', (0.5 * fade).toFixed(3));
        }
        if (dot) { dot.setAttribute('cx', pt.x); dot.setAttribute('cy', pt.y); dot.setAttribute('opacity', String(fade)); }
        if (glow) {
          glow.setAttribute('cx', pt.x);
          glow.setAttribute('cy', pt.y);
          glow.setAttribute('r', (8 + 3 * bell(p)).toFixed(1));
          glow.setAttribute('opacity', String(0.28 * fade));
        }
        if (label) {
          label.style.opacity = String(0.96 * fade);
          label.style.transform = 'translate(-50%,-50%) translateY(' + (-5 * fade).toFixed(1) + 'px)';
        }
        // the core flares as traffic passes through the middle of the graph
        coreAct = Math.max(coreAct, bell(p) * Math.max(0, 1 - Math.abs(pt.x - 650) / 260));
      });

      const halo = els['halo'];
      if (halo) {
        const q = Math.round(coreAct * 60);
        if (halo._q !== q) {
          halo._q = q;
          halo.style.opacity = (0.8 + 0.5 * coreAct).toFixed(3);
          halo.style.transform = 'scale(' + (1 + 0.07 * coreAct).toFixed(3) + ')';
        }
      }

      // wallets flash when a transfer leaves or lands on them — during the
      // intro sequence (edge timings) and the live replay (active list) alike
      const pulse = {};
      const bump = (id, k) => { if (k > 0.001) pulse[id] = Math.max(pulse[id] || 0, k); };
      edges.forEach((e) => {
        bump(e.from, bell((el - e.travelAt) / 0.4));
        bump(e.to, bell((el - e.landAt) / 0.5));
      });
      st.active.forEach((a) => {
        const hp = (t - a.t0) / 0.9;
        bump(a.e.from, bell(hp / 0.45));
        bump(a.e.to, bell((hp - 0.75) / 0.5));
      });
      nodes.forEach((n) => {
        const d = els['dot-' + n.id];
        const card = els['card-' + n.id];
        const k = pulse[n.id] || 0;
        const q = Math.round(k * 40);
        if (d && d._q !== q) {
          d._q = q;
          d.style.transform = 'scale(' + (1 + 0.45 * k).toFixed(3) + ')';
          d.style.boxShadow = '0 0 ' + (9 + 15 * k).toFixed(0) + 'px ' + n.color;
        }
        if (card) {
          // one transform writer: reveal pop × transaction bump
          const rk = clamp01((el - n.revealAt) / 0.38);
          const tf = 'scale(' + ((0.88 + 0.12 * backOut(rk)) * (1 + 0.035 * k)).toFixed(3) + ') translateY(' + ((1 - rk) * 10).toFixed(1) + 'px)';
          if (card._tf !== tf) { card._tf = tf; card.style.transform = tf; }
          // transaction highlight: the card floods with its tone color while funds move
          if (card._pq !== q) {
            card._pq = q;
            card.style.boxShadow = '0 10px 30px rgba(0,0,0,.5)' +
              (q > 0 ? ', 0 0 ' + (40 * k).toFixed(0) + 'px ' + n.glow + (0.55 * k).toFixed(2) + ')' : '');
            if (q > 0) {
              const frame = n.glow + (0.4 + 0.55 * k).toFixed(2) + ')';
              card.style.borderColor = frame + ' ' + frame + ' ' + frame + ' ' + n.color;
              card.style.background = 'linear-gradient(180deg, ' + n.glow + (0.16 + 0.44 * k).toFixed(2) + ') 0%, ' + n.glow + (0.05 + 0.22 * k).toFixed(2) + ') 100%)';
            } else {
              card.style.borderColor = n.frameRest;
              card.style.background = n.bgRest;
            }
          }
        }
      });

      STATS.forEach((s, i) => {
        const node = els['stat' + i];
        if (!node) return;
        const k = Math.min(1, el / 1.6);
        const drift = i === 1 ? Math.floor(el / 3) : Math.floor(el * (i === 0 ? 7 : 2));
        node.textContent = fmt(s.target * smooth(k) + (k >= 1 ? drift : 0));
      });

      raf = requestAnimationFrame(tick);
    };

    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [graph, txIntervalMs]);

  const setEl = (key) => (el) => { elsRef.current[key] = el; };

  return (
    <div
      ref={outerRef}
      className="ffh-hero w-full overflow-hidden rounded-xl border border-vaultrix-border"
      style={{ height: STAGE_H * scale || undefined }}
    >
      <div
        style={{
          position: 'relative',
          width: STAGE_W,
          height: STAGE_H,
          transform: `scale(${scale || 1})`,
          transformOrigin: 'top left',
          background: 'radial-gradient(120% 90% at 50% 42%, #0a1622 0%, #060A10 62%)',
          fontFamily: "'Space Grotesk', Helvetica, sans-serif",
          color: '#cbd5e1',
          overflow: 'hidden',
          userSelect: 'none',
          WebkitFontSmoothing: 'antialiased',
        }}
      >
        {/* scanlines */}
        <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none', zIndex: 40, opacity: 0.02, background: 'repeating-linear-gradient(to bottom, #ffffff 0px, #ffffff 1px, transparent 1px, transparent 3px)' }} />

        {/* vignette: sits under the content so it darkens only the backdrop, never card text */}
        <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none', zIndex: 2, background: 'radial-gradient(120% 100% at 50% 45%, transparent 62%, rgba(3,7,12,.45) 100%)' }} />

        {/* boot sweep: one bright line pass on mount */}
        <div style={{ position: 'absolute', left: 0, right: 0, top: 0, height: 3, zIndex: 35, pointerEvents: 'none', background: 'linear-gradient(90deg, transparent 0%, rgba(56,189,248,.5) 30%, rgba(125,211,252,.8) 50%, rgba(56,189,248,.5) 70%, transparent 100%)', filter: 'blur(1px)', animation: 'om-sweep 1.1s ease-out .15s both' }} />

        {/* ambient drifting motes */}
        {FLOAT_DOTS.map(([l, tp, s, d, dl], i) => (
          <div key={i} style={{ position: 'absolute', left: l + '%', top: tp + '%', width: s, height: s, borderRadius: '50%', background: 'rgba(125,211,252,.5)', boxShadow: '0 0 6px rgba(56,189,248,.4)', zIndex: 1, pointerEvents: 'none', animation: `om-float ${d}s ease-in-out ${dl}s infinite alternate` }} />
        ))}

        {/* HUD frame corners */}
        {HUD_CORNERS.map((c, i) => (
          <div key={i} style={{ position: 'absolute', width: 22, height: 22, borderStyle: 'solid', borderColor: 'rgba(56,189,248,.4)', pointerEvents: 'none', zIndex: 12, animation: 'om-title .5s ease .08s backwards', ...c }} />
        ))}

        {/* wallet intelligence CTA */}
        <button
          type="button"
          className="ffh-cta"
          onClick={onWalletIntelligence}
          style={{ position: 'absolute', right: 40, top: 34, zIndex: 30, animation: 'om-rise .5s cubic-bezier(.2,.8,.2,1) .08s backwards' }}
        >
          WALLET INTELLIGENCE
          <span className="ffh-cta-arrow">→</span>
        </button>

        {/* 1 — description */}
        <div style={{ position: 'absolute', left: 0, right: 0, top: 38, textAlign: 'center', zIndex: 10 }}>
          <div ref={setEl('headline')} style={{ fontSize: 26, fontWeight: 600, letterSpacing: '.22em', color: '#e2f4ff', animation: 'om-title .55s cubic-bezier(.2,.8,.2,1) both' }}>
            REAL-TIME FUND-FLOW INTELLIGENCE
          </div>
          <div style={{ maxWidth: 950, margin: '9px auto 0', fontSize: 15, lineHeight: 1.55, color: '#a8b8cc', animation: 'om-title .55s cubic-bezier(.2,.8,.2,1) .12s both' }}>
            CipherChain is a multi-chain crypto analysis platform that decrypts blockchain activity into living node
            graphs — tracing value flows wallet-to-wallet across Bitcoin, Ethereum and Tron, risk-scored and screened in real time.
          </div>
        </div>

        {/* 2 — module row */}
        <div style={{ position: 'absolute', left: 40, right: 40, top: 148, display: 'flex', gap: 14, zIndex: 10 }}>
          {MODULES.map((m, i) => (
            <div
              key={m.num}
              className={'ffh-module' + (i === activeModule ? ' ffh-module-active' : '')}
              style={{
                flex: '1 1 0', minWidth: 0, display: 'flex', alignItems: 'center', gap: 10, height: 72, boxSizing: 'border-box', padding: '0 14px',
                border: '1px solid #17222F', borderRadius: 10, background: 'rgba(10,15,22,0.75)', backdropFilter: 'blur(8px)',
                overflow: 'hidden',
                animation: `om-rise .45s cubic-bezier(.2,.8,.2,1) ${(MODULE_DELAY + i * MODULE_STEP).toFixed(2)}s backwards`,
              }}
            >
              <div className={'ffh-module-num' + (m.alert ? ' ffh-module-num-alert' : '')} style={{ width: 34, height: 34, flex: 'none', borderRadius: 8, border: `1px solid ${m.alert ? 'rgba(248,113,113,.35)' : 'rgba(56,189,248,.3)'}`, display: 'flex', alignItems: 'center', justifyContent: 'center', fontFamily: MONO, fontSize: 12, color: m.alert ? '#F87171' : '#38BDF8' }}>{m.num}</div>
              <div style={{ minWidth: 0, fontSize: 14, fontWeight: 600, color: '#f0f8ff', whiteSpace: 'nowrap' }}>{m.title}</div>
            </div>
          ))}
        </div>

        {/* 3 — trace graph */}
        <div style={{ position: 'absolute', left: 40, top: 296, width: 1218, display: 'flex', justifyContent: 'space-between', zIndex: 6, animation: `om-rise .5s cubic-bezier(.2,.8,.2,1) ${PANEL_DELAY}s backwards` }}>
          <div style={{ fontFamily: MONO, fontSize: 10.5, letterSpacing: '.2em', color: '#66788d' }}>SOURCE WALLETS</div>
          <div style={{ fontFamily: MONO, fontSize: 10.5, letterSpacing: '.2em', color: '#66788d' }}>INTERMEDIARIES · CORE</div>
          <div style={{ fontFamily: MONO, fontSize: 10.5, letterSpacing: '.2em', color: '#66788d' }}>DESTINATIONS</div>
        </div>

        <svg viewBox={`0 0 ${STAGE_W} ${STAGE_H}`} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none', zIndex: 4 }}>
          <defs>
            <linearGradient id="om-flow" gradientUnits="userSpaceOnUse" x1="230" y1="0" x2="1060" y2="0">
              <stop offset="0" stopColor="#38BDF8" stopOpacity=".18" />
              <stop offset="1" stopColor="#7dd3fc" stopOpacity=".85" />
            </linearGradient>
            <marker id="om-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#4fb6e0" />
            </marker>
            <marker id="om-arrow-hot" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7.5" markerHeight="7.5" orient="auto-start-reverse">
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#dff4ff" />
            </marker>
            <pattern id="om-grid" width="80" height="80" patternUnits="userSpaceOnUse">
              <path d="M 80 0 L 0 0 0 80" fill="none" stroke="#38BDF8" strokeOpacity=".05" strokeWidth="1" />
            </pattern>
          </defs>
          <rect x="40" y="290" width="1520" height="680" fill="url(#om-grid)" opacity="0.55" />
          <g stroke="#38BDF8" strokeWidth="1" strokeDasharray="3 6" strokeOpacity=".3" markerEnd="url(#om-arrow)" style={{ animation: 'om-dash 3s linear infinite' }}>
            <line x1="650" y1="509" x2="650" y2="523" />
            <line x1="650" y1="707" x2="650" y2="721" />
          </g>
          {graph.edges.map((e) => (
            <g key={e.id}>
              <path d={e.d} fill="none" stroke="url(#om-flow)" strokeWidth="1.2" strokeLinecap="round" markerEnd="url(#om-arrow)" style={{ opacity: 0 }} ref={setEl('line-' + e.id)} />
              <path d={e.d} fill="none" stroke="#aee2ff" strokeWidth="2.4" strokeLinecap="round" opacity="0" ref={setEl('t-' + e.id)} />
              <circle r="8" cx="-50" cy="-50" fill="#38BDF8" opacity="0" ref={setEl('g-' + e.id)} />
              <circle r="3.2" cx="-50" cy="-50" fill="#f2fbff" opacity="0" ref={setEl('p-' + e.id)} />
              <circle cx={e.ex2} cy={e.ey2} r="6" fill="none" stroke="#9ddcff" strokeWidth="1.6" opacity="0" ref={setEl('r-' + e.id)} />
            </g>
          ))}
        </svg>

        {/* core */}
        <div style={{ position: 'absolute', left: 650, top: 620, width: 190, height: 190, transform: 'translate(-50%,-50%)', zIndex: 3, animation: 'om-rise-core .7s cubic-bezier(.2,.8,.2,1) 1s both' }}>
          <div ref={setEl('halo')} style={{ position: 'absolute', inset: -120, borderRadius: '50%', background: 'radial-gradient(circle, rgba(56,189,248,.20) 0%, rgba(56,189,248,.05) 48%, transparent 72%)', filter: 'blur(4px)' }} />
          <div style={{ position: 'absolute', inset: -26, borderRadius: '50%', border: '1px solid rgba(56,189,248,.16)', animation: 'om-spin 30s linear infinite', background: 'conic-gradient(from 0deg, transparent 0deg, rgba(56,189,248,.12) 40deg, transparent 90deg)' }} />
          <div style={{ position: 'absolute', inset: -6, borderRadius: '50%', border: '1px dashed rgba(56,189,248,.16)', animation: 'om-spin-rev 44s linear infinite' }} />
          <div style={{ position: 'absolute', inset: -26, animation: 'om-spin 14s linear infinite' }}>
            <div style={{ position: 'absolute', left: '50%', top: -3, width: 6, height: 6, marginLeft: -3, borderRadius: '50%', background: '#7dd3fc', boxShadow: '0 0 10px #38BDF8' }} />
          </div>
          <div style={{ position: 'absolute', inset: -6, animation: 'om-spin-rev 21s linear infinite' }}>
            <div style={{ position: 'absolute', left: '50%', bottom: -2.5, width: 5, height: 5, marginLeft: -2.5, borderRadius: '50%', background: 'rgba(174,226,255,.9)', boxShadow: '0 0 8px #38BDF8' }} />
          </div>
          <div style={{ position: 'absolute', inset: 14, borderRadius: '50%', animation: 'om-breathe 6s ease-in-out infinite', background: 'radial-gradient(circle at 38% 32%, #cfeeff 0%, #5cc7f5 24%, #2b9fd4 46%, #0d4f76 72%, #051c2b 100%)', boxShadow: '0 0 40px rgba(56,189,248,.30), 0 0 110px rgba(56,189,248,.12), inset 0 0 40px rgba(2,20,32,.65)' }} />
          <div style={{ position: 'absolute', inset: 14, borderRadius: '50%', overflow: 'hidden', opacity: 0.16, mixBlendMode: 'screen' }}>
            <div style={{ position: 'absolute', inset: '-30%', animation: 'om-spin 18s linear infinite', background: 'repeating-conic-gradient(from 0deg, rgba(214,244,255,0) 0deg 7deg, rgba(214,244,255,.5) 7deg 7.4deg)' }} />
          </div>
          <div style={{ position: 'absolute', left: '50%', top: '50%', transform: 'translate(-50%,-50%)', textAlign: 'center' }}>
            <div style={{ fontFamily: MONO, fontSize: 11, letterSpacing: '.22em', color: '#041a27', fontWeight: 700 }}>CORE</div>
          </div>
        </div>

        {/* wallet cards */}
        {graph.nodes.map((n) => (
          <div
            key={n.id}
            ref={setEl('card-' + n.id)}
            className="ffh-card"
            style={{
              position: 'absolute', left: n.left, top: n.top, width: CARD_W, height: CARD_H, boxSizing: 'border-box',
              zIndex: 6, display: 'flex', alignItems: 'center', gap: 11, padding: '0 14px',
              borderStyle: 'solid', borderWidth: '1px 1px 1px 2px', borderColor: n.frameRest, borderRadius: 7,
              background: n.bgRest,
              boxShadow: '0 10px 30px rgba(0,0,0,.5)', opacity: 0, transform: 'scale(.88) translateY(10px)',
            }}
          >
            <div ref={setEl('dot-' + n.id)} style={{ width: 9, height: 9, flex: 'none', borderRadius: '50%', background: n.color, boxShadow: `0 0 10px ${n.color}` }} />
            <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 5 }}>
              <div style={{ fontFamily: MONO, fontSize: 14, fontWeight: 700, letterSpacing: '.02em', color: '#ffffff', textShadow: '0 1px 2px rgba(0,0,0,.85)' }}>{n.addr}</div>
              <div style={{ fontFamily: MONO, fontSize: 10, letterSpacing: '.1em', textTransform: 'uppercase', color: '#c6d4e2' }}>{n.role}</div>
              <div style={{ fontFamily: MONO, fontSize: 11, fontWeight: 600, letterSpacing: '.04em', color: '#7fd6f8', textShadow: '0 1px 2px rgba(0,0,0,.7)' }}>{n.volume}</div>
            </div>
          </div>
        ))}

        {/* travelling transfer labels */}
        {graph.edges.map((e) => (
          <div
            key={e.id}
            ref={setEl('l-' + e.id)}
            style={{
              position: 'absolute', left: e.mx, top: e.my, transform: 'translate(-50%,-50%)', zIndex: 8,
              opacity: 0, pointerEvents: 'none', display: 'flex', alignItems: 'center', gap: 7,
              padding: '4px 10px', borderRadius: 4, border: '1px solid rgba(56,189,248,.45)',
              background: 'rgba(6,14,21,.97)', whiteSpace: 'nowrap',
            }}
          >
            <span style={{ fontFamily: MONO, fontSize: 11, color: '#a8cde2' }}>{e.fromAddr}</span>
            <span style={{ fontFamily: MONO, fontSize: 11, color: '#38BDF8' }}>→</span>
            <span style={{ fontFamily: MONO, fontSize: 11, color: '#a8cde2' }}>{e.toAddr}</span>
            <span style={{ fontFamily: MONO, fontSize: 11.5, fontWeight: 600, color: '#f2fbff', borderLeft: '1px solid rgba(56,189,248,.3)', paddingLeft: 7 }}>{e.value}</span>
          </div>
        ))}

        {/* pipeline + transfer log */}
        <div style={{ position: 'absolute', right: 40, top: 290, width: 270, boxSizing: 'border-box', padding: 18, border: '1px solid #17222F', borderRadius: 10, background: 'rgba(10,15,22,0.75)', backdropFilter: 'blur(8px)', zIndex: 10, animation: `om-rise .5s cubic-bezier(.2,.8,.2,1) ${PANEL_DELAY}s backwards` }}>
          <div style={{ fontFamily: MONO, fontSize: 10.5, letterSpacing: '.2em', color: '#66788d', marginBottom: 16 }}>PIPELINE STATUS</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {PIPELINE.map((p) => (
              <div key={p.text} style={{ display: 'flex', gap: 11, alignItems: 'flex-start' }}>
                <div style={{ width: 8, height: 8, marginTop: 5, flex: 'none', borderRadius: '50%', background: p.color, boxShadow: `0 0 10px ${p.color}`, animation: p.blip ? `om-blip ${p.blip} ease-in-out infinite` : undefined }} />
                <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
                  <div style={{ fontFamily: MONO, fontSize: 12, color: '#e8f1fb' }}>{p.text}</div>
                  <div style={{ fontFamily: MONO, fontSize: 10, letterSpacing: '.08em', color: p.color }}>{p.meta}</div>
                </div>
              </div>
            ))}
          </div>
          <div style={{ height: 1, background: '#17222F', margin: '18px -18px' }} />
          <div style={{ fontFamily: MONO, fontSize: 10.5, letterSpacing: '.2em', color: '#66788d', marginBottom: 12 }}>TRANSFER LOG</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 9, minHeight: 108 }}>
            {log.map((r, i) => (
              <div key={r.key} style={{ display: 'flex', flexDirection: 'column', gap: 2, opacity: r.opacity, animation: i === 0 ? 'om-label-in .35s ease both' : undefined }}>
                <div style={{ fontFamily: MONO, fontSize: 11.5, color: '#dde8f4' }}>{r.pair}</div>
                <div style={{ fontFamily: MONO, fontSize: 10, letterSpacing: '.06em', color: '#4fc3f7' }}>{r.meta}</div>
              </div>
            ))}
          </div>
        </div>

        {/* equalizer */}
        <div style={{ position: 'absolute', left: '50%', bottom: 44, transform: 'translateX(-50%)', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12, zIndex: 10, animation: 'om-rise-cx .5s cubic-bezier(.2,.8,.2,1) 1.1s both' }}>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 4, height: 40 }}>
            {BARS.map(([dur, delay], i) => (
              <div key={i} style={{ width: 4, height: 40, borderRadius: 2, background: 'linear-gradient(to top, rgba(56,189,248,.2), #38BDF8)', transformOrigin: 'bottom', animation: `om-bar ${dur}s ease-in-out ${delay}s infinite` }} />
            ))}
          </div>
          <div ref={setEl('eqcap')} style={{ fontFamily: MONO, fontSize: 10.5, letterSpacing: '.2em', color: '#8598ad' }}>MODEL ACTIVE · ANALYZING LIVE DATA</div>
        </div>

        {/* stats */}
        <div style={{ position: 'absolute', right: 40, bottom: 44, display: 'flex', gap: 26, padding: '16px 20px', border: '1px solid #17222F', borderRadius: 10, background: 'rgba(10,15,22,0.75)', backdropFilter: 'blur(8px)', zIndex: 10, animation: 'om-rise .5s cubic-bezier(.2,.8,.2,1) 1s backwards' }}>
          {STATS.map((s, i) => (
            <React.Fragment key={s.label}>
              {i > 0 && <div style={{ width: 1, background: '#17222F' }} />}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div ref={setEl('stat' + i)} style={{ fontFamily: MONO, fontSize: 20, fontWeight: 700, color: '#f2fbff' }}>0</div>
                <div style={{ fontFamily: MONO, fontSize: 9.5, letterSpacing: '.14em', color: '#7e90a4' }}>{s.label}</div>
              </div>
            </React.Fragment>
          ))}
        </div>

        {/* legend */}
        <div style={{ position: 'absolute', left: 40, bottom: 44, display: 'flex', flexDirection: 'column', gap: 10, zIndex: 10, animation: 'om-rise .5s cubic-bezier(.2,.8,.2,1) 1.05s backwards' }}>
          <div style={{ fontFamily: MONO, fontSize: 10.5, letterSpacing: '.2em', color: '#66788d' }}>LEGEND</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
            {LEGEND.map((l) => (
              <div key={l.label} style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                <div style={{ width: 9, height: 9, borderRadius: '50%', background: l.color, boxShadow: `0 0 8px ${l.color}` }} />
                <div style={{ fontFamily: MONO, fontSize: 11, color: '#adbccb' }}>{l.label}</div>
              </div>
            ))}
            <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginTop: 3 }}>
              <svg width="26" height="9" viewBox="0 0 26 9">
                <line x1="0" y1="4.5" x2="19" y2="4.5" stroke="#38BDF8" strokeWidth="1.2" />
                <path d="M19 1 L25 4.5 L19 8 z" fill="#38BDF8" />
              </svg>
              <div style={{ fontFamily: MONO, fontSize: 11, color: '#adbccb' }}>FLOW DIRECTION (BRIGHTER = DESTINATION)</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
