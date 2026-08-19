import React, { Suspense, useEffect, useMemo, useRef } from 'react';
import { Canvas, useFrame, useLoader, useThree } from '@react-three/fiber';
import * as THREE from 'three';

/**
 * Professional CipherChain coin orbit —
 * face-forward logos, smooth gather → hold → expand, elegant rigid spin.
 */
const COIN_DEFS = [
  { id: 'btc', src: '/coins/btc.png', glow: '#3DDCFF' },
  { id: 'eth', src: '/coins/eth.png', glow: '#F5C542' },
  { id: 'sol', src: '/coins/sol.png', glow: '#7CF5E0' },
  { id: 'usdt', src: '/coins/usdt.png', glow: '#50E3C2' },
  { id: 'bnb', src: '/coins/bnb.png', glow: '#F0B90B' },
  { id: 'trx', src: '/coins/trx.png', glow: '#C44DFF' },
  { id: 'apt', src: '/coins/apt.png', glow: '#C8CCD4' },
  { id: 'xrp', src: '/coins/xrp.png', glow: '#8B9BFF' },
  { id: 'ton', src: '/coins/ton.png', glow: '#4DA8FF' },
  { id: 'avax', src: '/coins/avax.png', glow: '#FF5C7A' },
  { id: 'ilv', src: '/coins/ilv.png', glow: '#B07CFF' }
];

const GATHER_END = 1.8;
const HOLD_END = 2.55;
const EXPAND_END = 4.4;
const CYCLE = 5.6;
const ORBIT_SPEED = 0.16;

function easeOutCubic(t) {
  const x = Math.min(1, Math.max(0, t));
  return 1 - (1 - x) ** 3;
}

function easeInOutCubic(t) {
  const x = Math.min(1, Math.max(0, t));
  return x < 0.5 ? 4 * x * x * x : 1 - (-2 * x + 2) ** 3 / 2;
}

function easeInOutQuint(t) {
  const x = Math.min(1, Math.max(0, t));
  return x < 0.5 ? 16 * x * x * x * x * x : 1 - (-2 * x + 2) ** 5 / 2;
}

function cycleState(cycleT) {
  const SPREAD_OUT = 1.18;
  const SPREAD_IN = 0.84;
  const SPREAD_EXPANDED = 1.08;

  if (cycleT < GATHER_END) {
    const k = easeInOutQuint(cycleT / GATHER_END);
    return {
      appear: easeOutCubic(Math.min(1, cycleT / 0.55)),
      spread: THREE.MathUtils.lerp(SPREAD_OUT, SPREAD_IN, k),
      depth: THREE.MathUtils.lerp(0.2, 0, k),
      glow: 0.55 + k * 0.45
    };
  }
  if (cycleT < HOLD_END) {
    return { appear: 1, spread: SPREAD_IN, depth: 0, glow: 1 };
  }
  if (cycleT < EXPAND_END) {
    const k = easeInOutQuint((cycleT - HOLD_END) / (EXPAND_END - HOLD_END));
    return {
      appear: 1,
      spread: THREE.MathUtils.lerp(SPREAD_IN, SPREAD_EXPANDED, k),
      depth: k,
      glow: 1 - k * 0.15
    };
  }
  const k = easeInOutCubic((cycleT - EXPAND_END) / (CYCLE - EXPAND_END));
  return {
    appear: 1,
    spread: THREE.MathUtils.lerp(SPREAD_EXPANDED, SPREAD_OUT, k),
    depth: 1 - k * 0.35,
    glow: 0.85
  };
}

function ImageCoin({
  index,
  count,
  def,
  texture,
  getRingRadii,
  size,
  radiusMul,
  zBias,
  floatPhase
}) {
  const group = useRef(null);
  const coin = useRef(null);
  const mat = useRef(null);
  const glowMat = useRef(null);
  const rimMat = useRef(null);

  const slotAngle = (index / count) * Math.PI * 2 - Math.PI / 2;
  // Staggered entrance without breaking formation
  const appearOffset = (index / count) * 0.28;

  useFrame((state, delta) => {
    const t = state.clock.elapsedTime;
    const cycleT = t % CYCLE;
    const { appear, spread, depth, glow } = cycleState(cycleT);
    const localAppear = Math.min(1, Math.max(0, (appear * 1.15) - appearOffset));
    const { rx, ry } = getRingRadii();

    const orbitAngle = slotAngle + t * ORBIT_SPEED;
    const floatY = Math.sin(t * 0.55 + floatPhase) * 0.045 * localAppear;

    const x = Math.cos(orbitAngle) * rx * radiusMul * spread;
    const y = Math.sin(orbitAngle) * ry * radiusMul * spread + floatY;
    const zNear = 0.35 + zBias * 0.1;
    const zFar = -0.85 + zBias * 0.85;
    const z = THREE.MathUtils.lerp(zNear, zFar, depth);

    if (group.current) {
      const target = group.current.userData.target || { x, y, z };
      const blend = 1 - Math.exp(-delta * 5.5);
      target.x = THREE.MathUtils.lerp(target.x || x, x, blend);
      target.y = THREE.MathUtils.lerp(target.y || y, y, blend);
      target.z = THREE.MathUtils.lerp(target.z || z, z, blend);
      group.current.userData.target = target;
      group.current.position.set(target.x, target.y, target.z);
      group.current.scale.setScalar(size * (0.72 + localAppear * 0.28));
      group.current.visible = localAppear > 0.02;
    }

    if (coin.current) {
      coin.current.rotation.set(0, 0, 0);
    }

    if (mat.current) mat.current.opacity = localAppear;
    if (glowMat.current) glowMat.current.opacity = 0.08 * localAppear * glow;
    if (rimMat.current) rimMat.current.opacity = 0.28 * localAppear * glow;
  });

  return (
    <group ref={group}>
      <group ref={coin}>
        <mesh rotation={[Math.PI / 2, 0, 0]} scale={[1, 1, 0.28]}>
          <torusGeometry args={[0.9, 0.028, 10, 48]} />
          <meshBasicMaterial
            ref={rimMat}
            color={def.glow}
            transparent
            opacity={0}
            depthWrite={false}
            toneMapped={false}
            blending={THREE.AdditiveBlending}
          />
        </mesh>

        <mesh>
          <planeGeometry args={[2, 2]} />
          <meshBasicMaterial
            ref={mat}
            map={texture}
            transparent
            opacity={0}
            depthWrite={false}
            side={THREE.DoubleSide}
            toneMapped={false}
          />
        </mesh>

        <mesh scale={1.06} position={[0, 0, -0.025]}>
          <circleGeometry args={[0.88, 40]} />
          <meshBasicMaterial
            ref={glowMat}
            color={def.glow}
            transparent
            opacity={0}
            depthWrite={false}
            toneMapped={false}
            blending={THREE.AdditiveBlending}
          />
        </mesh>
      </group>
    </group>
  );
}

function useSafeRingRadii() {
  const { viewport } = useThree();
  return useMemo(() => {
    const halfW = viewport.width * 0.5;
    const halfH = viewport.height * 0.5;
    const rx = Math.min(halfW * 0.56, Math.max(2.55, halfW * 0.52));
    const ry = Math.min(halfH * 0.6, Math.max(2.3, halfH * 0.5));
    return { rx: Math.max(2.55, rx), ry: Math.max(2.3, ry) };
  }, [viewport.width, viewport.height]);
}

function MouseParallax({ children }) {
  const group = useRef(null);
  const target = useRef({ x: 0, y: 0 });

  useEffect(() => {
    const onMove = (e) => {
      target.current.x = (e.clientX / window.innerWidth) * 2 - 1;
      target.current.y = (e.clientY / window.innerHeight) * 2 - 1;
    };
    window.addEventListener('mousemove', onMove, { passive: true });
    return () => window.removeEventListener('mousemove', onMove);
  }, []);

  useFrame((_, delta) => {
    if (!group.current) return;
    const k = 1 - Math.exp(-delta * 2.4);
    group.current.rotation.y = THREE.MathUtils.lerp(
      group.current.rotation.y,
      target.current.x * 0.07,
      k
    );
    group.current.rotation.x = THREE.MathUtils.lerp(
      group.current.rotation.x,
      -target.current.y * 0.05,
      k
    );
  });

  return <group ref={group}>{children}</group>;
}

function CoinTextures({ children }) {
  const urls = useMemo(() => COIN_DEFS.map((c) => c.src), []);
  const textures = useLoader(THREE.TextureLoader, urls);

  useEffect(() => {
    const list = Array.isArray(textures) ? textures : [textures];
    list.forEach((tex) => {
      tex.colorSpace = THREE.SRGBColorSpace;
      tex.anisotropy = 8;
      tex.needsUpdate = true;
    });
  }, [textures]);

  const list = Array.isArray(textures) ? textures : [textures];
  return children(list);
}

function OrbitRing() {
  const radii = useSafeRingRadii();
  const radiiRef = useRef(radii);
  radiiRef.current = radii;
  const getRingRadii = useMemo(() => () => radiiRef.current, []);

  const coins = useMemo(() => {
    return COIN_DEFS.map((def, i) => ({
      def,
      index: i,
      count: COIN_DEFS.length,
      size: 0.48 + (i % 4) * 0.035,
      radiusMul: 0.93 + (i % 3) * 0.05,
      zBias: ((i % 4) - 1.5) * 0.22,
      floatPhase: i * 0.7
    }));
  }, []);

  return (
    <MouseParallax>
      <mesh position={[0, 0, -3.4]}>
        <circleGeometry args={[2.8, 64]} />
        <meshBasicMaterial color="#1a6dff" transparent opacity={0.09} depthWrite={false} />
      </mesh>
      <mesh position={[0, 0, -3.0]}>
        <circleGeometry args={[1.35, 64]} />
        <meshBasicMaterial color="#3db8ff" transparent opacity={0.06} depthWrite={false} />
      </mesh>

      <CoinTextures>
        {(textures) =>
          coins.map((c) => (
            <ImageCoin
              key={c.def.id}
              {...c}
              texture={textures[c.index]}
              getRingRadii={getRingRadii}
            />
          ))
        }
      </CoinTextures>
    </MouseParallax>
  );
}

const OrbitingCoins = () => {
  return (
    <div className="orbit-coins-stage" aria-hidden="true">
      <div className="orbit-center-glow" />
      <div className="orbit-edge-fade" />
      <Canvas
        dpr={[1, 1.6]}
        camera={{ position: [0, 0, 8.6], fov: 36, near: 0.1, far: 40 }}
        gl={{ antialias: true, alpha: true, powerPreference: 'high-performance' }}
        onCreated={({ gl }) => {
          gl.setClearColor(0x000000, 0);
          gl.toneMapping = THREE.ACESFilmicToneMapping;
          gl.toneMappingExposure = 1.1;
        }}
        style={{ width: '100%', height: '100%', background: 'transparent' }}
      >
        <ambientLight intensity={0.9} />
        <Suspense fallback={null}>
          <OrbitRing />
        </Suspense>
      </Canvas>
    </div>
  );
};

export default OrbitingCoins;
