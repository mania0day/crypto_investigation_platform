import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import * as THREE from 'three';
import './cipherCore.css';

/* ── Input sources (left rail) ─────────────────────────────────────────── */
const SOURCES = [
  { icon: '🔐', label: 'Transaction Request' },
  { icon: '🧾', label: 'Wallet Signature' },
  { icon: '🌐', label: 'Network Node Sync' },
  { icon: '🛡️', label: 'Encryption Layer' },
  { icon: '📶', label: 'Ledger Validation' },
  { icon: '🧠', label: 'Smart Contract Check' },
  { icon: '📍', label: 'Node Location Verify' },
];

const SCAN_STAGES = ['Encrypting…', 'Validating Block…', 'Transaction Confirmed ✅'];

/* ── Shaders for the glowing node cluster ──────────────────────────────── */
const POINT_VERT = /* glsl */ `
  uniform float uTime;
  uniform float uWave;      // -1 = idle, else 0..>1 expanding ripple front
  uniform float uRadius;
  uniform float uPixelRatio;
  uniform float uHeart;     // 0..1 heartbeat envelope
  attribute float aRand;
  varying float vBright;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    float dNorm = length(position) / uRadius;
    float tw = 0.55 + 0.45 * sin(uTime * 1.6 + aRand * 6.2831);
    float band = (1.0 - smoothstep(0.0, 0.16, abs(dNorm - uWave))) * step(0.0, uWave);
    vBright = tw * (0.7 + 0.3 * uHeart) + band * 1.8;
    float size = (1.7 + band * 4.6 + uHeart * 0.8) * uPixelRatio;
    gl_PointSize = size * (330.0 / max(0.001, -mv.z));
    gl_Position = projectionMatrix * mv;
  }
`;

const POINT_FRAG = /* glsl */ `
  precision mediump float;
  varying float vBright;
  uniform vec3 uColor;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float a = smoothstep(0.5, 0.0, d);
    vec3 hot = vec3(0.85, 0.98, 1.0);
    vec3 col = mix(uColor, hot, clamp(vBright - 0.8, 0.0, 1.0) * 0.6) * vBright;
    gl_FragColor = vec4(col, a);
  }
`;

function heartbeat(t) {
  const p = (t % 3) / 3;
  const b1 = Math.exp(-Math.pow((p - 0.1) / 0.05, 2));
  const b2 = Math.exp(-Math.pow((p - 0.22) / 0.06, 2));
  return Math.min(1, b1 + 0.7 * b2);
}

/* ── The rotating node sphere ("Cipher Core") ──────────────────────────── */
function CoreCluster({ pulseId, count = 210, radius = 2.15 }) {
  const groupRef = useRef();

  const { pointsGeo, linesGeo, pts } = useMemo(() => {
    const positions = new Float32Array(count * 3);
    const rands = new Float32Array(count);
    const pts = [];
    const golden = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < count; i++) {
      const y = 1 - (i / (count - 1)) * 2;
      const r = Math.sqrt(Math.max(0, 1 - y * y));
      const theta = golden * i;
      const rr = radius * (0.85 + 0.15 * Math.random());
      const px = Math.cos(theta) * r * rr;
      const py = y * rr;
      const pz = Math.sin(theta) * r * rr;
      positions.set([px, py, pz], i * 3);
      rands[i] = Math.random();
      pts.push(new THREE.Vector3(px, py, pz));
    }
    const seg = [];
    const K = 3;
    const thresh = radius * 0.55;
    for (let i = 0; i < pts.length; i++) {
      const near = [];
      for (let j = 0; j < pts.length; j++) {
        if (i === j) continue;
        const d = pts[i].distanceTo(pts[j]);
        if (d < thresh) near.push([d, j]);
      }
      near.sort((a, b) => a[0] - b[0]);
      for (let k = 0; k < Math.min(K, near.length); k++) {
        const j = near[k][1];
        if (j > i) seg.push(pts[i].x, pts[i].y, pts[i].z, pts[j].x, pts[j].y, pts[j].z);
      }
    }
    const pointsGeo = new THREE.BufferGeometry();
    pointsGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    pointsGeo.setAttribute('aRand', new THREE.BufferAttribute(rands, 1));
    const linesGeo = new THREE.BufferGeometry();
    linesGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(seg), 3));
    return { pointsGeo, linesGeo, pts };
  }, [count, radius]);

  const uniforms = useMemo(
    () => ({
      uTime: { value: 0 },
      uWave: { value: -1 },
      uRadius: { value: radius },
      uPixelRatio: { value: Math.min(2, typeof window !== 'undefined' ? window.devicePixelRatio : 1) },
      uHeart: { value: 0 },
      uColor: { value: new THREE.Color('#38e8ff') },
    }),
    [radius],
  );

  const pointsMat = useMemo(
    () =>
      new THREE.ShaderMaterial({
        uniforms,
        vertexShader: POINT_VERT,
        fragmentShader: POINT_FRAG,
        transparent: true,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      }),
    [uniforms],
  );
  const linesMat = useMemo(
    () =>
      new THREE.LineBasicMaterial({
        color: new THREE.Color('#2f8fd0'),
        transparent: true,
        opacity: 0.2,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      }),
    [],
  );

  // "block confirmation": a bright line between two random nodes that fades out
  const confGeo = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(6), 3));
    return g;
  }, []);
  const confMat = useMemo(
    () =>
      new THREE.LineBasicMaterial({
        color: new THREE.Color('#a8f4ff'),
        transparent: true,
        opacity: 0,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      }),
    [],
  );

  const wave = useRef(-1);
  const confLife = useRef(0);

  // a transaction fired → kick off ripple + confirmation line
  useEffect(() => {
    if (pulseId <= 0) return;
    wave.current = 0;
    const a = pts[(Math.random() * pts.length) | 0];
    const b = pts[(Math.random() * pts.length) | 0];
    const arr = confGeo.attributes.position.array;
    arr[0] = a.x; arr[1] = a.y; arr[2] = a.z;
    arr[3] = b.x; arr[4] = b.y; arr[5] = b.z;
    confGeo.attributes.position.needsUpdate = true;
    confLife.current = 1;
  }, [pulseId, pts, confGeo]);

  useFrame((state, dt) => {
    const t = state.clock.elapsedTime;
    const g = groupRef.current;
    if (!g) return;

    uniforms.uTime.value = t;
    g.rotation.y += dt * 0.12;
    g.rotation.x = Math.sin(t * 0.15) * 0.06;

    const beat = heartbeat(t);
    uniforms.uHeart.value = beat;
    g.scale.setScalar(1 + beat * 0.03);

    if (wave.current >= 0) {
      wave.current += dt / 1.2;
      if (wave.current > 1.2) wave.current = -1;
    }
    uniforms.uWave.value = wave.current;

    linesMat.opacity = 0.14 + beat * 0.1 + (wave.current >= 0 ? 0.2 * (1 - wave.current) : 0);

    if (confLife.current > 0) {
      confLife.current -= dt / 0.9;
      confMat.opacity = Math.max(0, confLife.current) * 0.9;
    }
  });

  return (
    <group ref={groupRef}>
      <points geometry={pointsGeo} material={pointsMat} />
      <lineSegments geometry={linesGeo} material={linesMat} />
      <lineSegments geometry={confGeo} material={confMat} />
    </group>
  );
}

/* ── Force the renderer size (R3F auto-measure is unreliable inside an
   absolutely-positioned layer, so we drive it from the measured container). ── */
function Resizer({ w, h }) {
  const gl = useThree((s) => s.gl);
  const camera = useThree((s) => s.camera);
  const setSize = useThree((s) => s.setSize);
  useEffect(() => {
    if (w > 0 && h > 0) {
      setSize(w, h);
      if (camera.isPerspectiveCamera) {
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
      }
    }
  }, [w, h, gl, camera, setSize]);
  return null;
}

/* ── Cinematic camera dolly-in + idle parallax ─────────────────────────── */
function CameraRig() {
  const { camera } = useThree();
  const start = useRef(null);
  useFrame((state) => {
    const t = state.clock.elapsedTime;
    if (start.current === null) start.current = t;
    const e = t - start.current;
    const intro = Math.min(1, e / 8);
    const eased = 1 - Math.pow(1 - intro, 3);
    camera.position.z = 9.4 - eased * 3.0 + Math.sin(t * 0.4) * 0.12;
    camera.position.x = Math.sin(t * 0.12) * 0.5;
    camera.position.y = Math.cos(t * 0.16) * 0.3;
    camera.lookAt(0, 0, 0);
  });
  return null;
}

/* ── Full dashboard hero: 3D core + SVG light-trails + HUD overlays ────── */
export default function CipherCore() {
  const wrapRef = useRef(null);
  const [dims, setDims] = useState({ w: 1100, h: 600 });
  const [measured, setMeasured] = useState(false);
  const [beat, setBeat] = useState(0);
  const [pulseId, setPulseId] = useState(0);
  const [scanStage, setScanStage] = useState(0);
  const [processing, setProcessing] = useState(12);
  const [blocks, setBlocks] = useState(1204532);

  const activeSource = beat % SOURCES.length;

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => {
      setDims({ w: el.clientWidth, h: el.clientHeight });
      setMeasured(true);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // R3F sizes itself from a ResizeObserver; nudge it with a window resize so it
  // measures synchronously even where RO delivery is unreliable.
  useEffect(() => {
    if (!measured) return undefined;
    const nudge = () => window.dispatchEvent(new Event('resize'));
    const raf = requestAnimationFrame(nudge);
    const t = setTimeout(nudge, 150);
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(t);
    };
  }, [measured, dims.w, dims.h]);

  // transaction cycle: fire a validation every ~2.6s
  useEffect(() => {
    const id = setInterval(() => {
      setBeat((b) => b + 1);
      setPulseId((p) => p + 1);
      setScanStage((s) => (s + 1) % SCAN_STAGES.length);
      setBlocks((b) => b + 7 + Math.floor(Math.random() * 44));
    }, 2600);
    return () => clearInterval(id);
  }, []);

  // processing bar ticks 0→100 then loops
  useEffect(() => {
    const id = setInterval(
      () => setProcessing((p) => (p >= 100 ? 0 : p + 1 + Math.floor(Math.random() * 3))),
      120,
    );
    return () => clearInterval(id);
  }, []);

  const geo = useMemo(() => {
    const { w, h } = dims;
    const cx = w * 0.5;
    const cy = h * 0.52;
    const leftX = Math.max(196, w * 0.18);
    const rightX = Math.min(w - 60, w * 0.82);
    const n = SOURCES.length;
    const spread = Math.min(h * 0.72, 430);
    const top = cy - spread / 2;
    const rows = SOURCES.map((_, i) => top + (spread / (n - 1)) * i);
    const trails = rows.map((y) => {
      const c1x = leftX + (cx - leftX) * 0.42;
      const c2x = cx - (cx - leftX) * 0.22;
      const c2y = cy + (y - cy) * 0.15;
      return `M ${leftX} ${y} C ${c1x} ${y}, ${c2x} ${c2y}, ${cx} ${cy}`;
    });
    return { cx, cy, leftX, rightX, rows, trails };
  }, [dims]);

  return (
    <div ref={wrapRef} className="cc-wrap">
      <div className="cc-bg" />
      <div className="cc-grid" />
      <div className="cc-coreglow" style={{ left: geo.cx, top: geo.cy }} />

      <div className="cc-canvas">
        {measured && (
          <Canvas
            style={{ width: dims.w, height: dims.h }}
            camera={{ position: [0, 0, 9.4], fov: 50 }}
            dpr={[1, 2]}
            gl={{ antialias: true, alpha: true, powerPreference: 'high-performance' }}
            resize={{ scroll: false, debounce: 0 }}
            onCreated={(state) => state.setSize(dims.w, dims.h)}
          >
            <Resizer w={dims.w} h={dims.h} />
            <CameraRig />
            <CoreCluster pulseId={pulseId} />
          </Canvas>
        )}
      </div>

      {/* light-trails from input sources into the core */}
      <svg className="cc-svg" width={dims.w} height={dims.h} viewBox={`0 0 ${dims.w} ${dims.h}`}>
        <defs>
          <linearGradient id="cc-trail-grad" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#1f7fbf" stopOpacity="0.15" />
            <stop offset="100%" stopColor="#35e0ff" stopOpacity="0.85" />
          </linearGradient>
          <radialGradient id="cc-particle" cx="0.5" cy="0.5" r="0.5">
            <stop offset="0%" stopColor="#ffffff" />
            <stop offset="45%" stopColor="#5fe9ff" />
            <stop offset="100%" stopColor="#35e0ff" stopOpacity="0" />
          </radialGradient>
          <filter id="cc-blur" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="2.4" />
          </filter>
        </defs>

        {/* output beam: core → verified orb */}
        <path
          d={`M ${geo.cx} ${geo.cy} L ${geo.rightX} ${geo.cy}`}
          stroke="#35e0ff"
          strokeWidth="2"
          fill="none"
          strokeLinecap="round"
          opacity="0.15"
        >
          <animate attributeName="opacity" values="0.1;0.85;0.1" dur="2.6s" repeatCount="indefinite" />
        </path>

        {geo.trails.map((d, i) => (
          <g key={i}>
            <path d={d} stroke="url(#cc-trail-grad)" strokeWidth="1.4" fill="none" opacity="0.5" />
            <path
              d={d}
              stroke="#35e0ff"
              strokeWidth="1.5"
              fill="none"
              strokeDasharray="5 11"
              opacity="0.55"
              className="cc-flow"
            />
            <path id={`cc-trail-${i}`} d={d} stroke="none" fill="none" />
            <circle r="3.2" fill="url(#cc-particle)" filter="url(#cc-blur)">
              <animateMotion dur="2.6s" begin={`${i * 0.34}s`} repeatCount="indefinite" rotate="auto">
                <mpath href={`#cc-trail-${i}`} xlinkHref={`#cc-trail-${i}`} />
              </animateMotion>
            </circle>
          </g>
        ))}
      </svg>

      {/* top-left text panel */}
      <div className="cc-panel">
        <h2>CIPHER CHAIN</h2>
        <div className="cc-tag">Decentralized · Encrypted · Verified</div>
        <p>Real-time blockchain transaction visualization powered by adaptive cryptographic nodes.</p>
      </div>

      {/* input source chips (left rail) */}
      {SOURCES.map((s, i) => (
        <div
          key={s.label}
          className={`cc-chip${i === activeSource ? ' cc-active' : ''}`}
          style={{ top: geo.rows[i], left: 22 }}
        >
          <span className="cc-em">{s.icon}</span>
          <span>{s.label}</span>
        </div>
      ))}

      {/* core label */}
      <div className="cc-corelabel" style={{ left: geo.cx, top: geo.cy + 150 }}>
        CIPHER CORE — BLOCKCHAIN ENGINE
      </div>

      {/* verified output orb (right) */}
      <div className="cc-orb" style={{ left: geo.rightX + 34, top: geo.cy }}>
        <div className="cc-ring">
          <svg width="118" height="118" viewBox="0 0 118 118">
            <circle cx="59" cy="59" r="54" fill="none" stroke="rgba(0,240,255,0.14)" strokeWidth="1" />
            <g className="cc-ring-rot">
              <circle
                cx="59"
                cy="59"
                r="48"
                fill="none"
                stroke="#35e0ff"
                strokeWidth="1.6"
                strokeDasharray="6 12"
                opacity="0.75"
              />
            </g>
            <path
              d="M59 32 L78 41 V60 C78 74 70 82 59 87 C48 82 40 74 40 60 V41 Z"
              fill="rgba(53,224,255,0.12)"
              stroke="#7ef0ff"
              strokeWidth="1.6"
            />
            <path d="M51 59 l6 6 l11 -13" fill="none" stroke="#43ffd0" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>
        <div className="cc-orb-title">SECURE TRANSACTION — VERIFIED</div>
        <div className="cc-scanlog">
          {SCAN_STAGES.map((m, i) => (
            <div key={m} className={i === scanStage ? 'cc-on' : undefined}>
              {m}
            </div>
          ))}
        </div>
      </div>

      {/* bottom HUD */}
      <div className="cc-hud">
        <div className="cc-hud-block">
          <div className="cc-hud-label">Processing</div>
          <div className="cc-bar">
            <span style={{ width: `${processing}%` }} />
          </div>
          <div className="cc-pct">{String(processing).padStart(2, '0')}%</div>
        </div>

        <div className="cc-hud-block">
          <div className="cc-hud-label">Learning &amp; Optimizing</div>
          <div className="cc-wave">
            {Array.from({ length: 26 }).map((_, i) => (
              <i key={i} style={{ animationDelay: `${i * 0.055}s` }} />
            ))}
          </div>
        </div>

        <div className="cc-hud-block cc-ticker">
          <div className="cc-hud-label">Network</div>
          <div>
            Blocks synced: <b>{blocks.toLocaleString()}</b>
          </div>
        </div>
      </div>
    </div>
  );
}
