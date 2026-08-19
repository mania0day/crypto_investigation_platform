import React, { Suspense, useRef } from 'react';
import { Canvas, useFrame } from '@react-three/fiber';
import { Float } from '@react-three/drei';

function GlowingCube() {
  const meshRef = useRef(null);

  useFrame((_, delta) => {
    if (!meshRef.current) return;
    meshRef.current.rotation.x += delta * 0.35;
    meshRef.current.rotation.y += delta * 0.55;
  });

  return (
    <Float speed={1.2} rotationIntensity={0.15} floatIntensity={0.4}>
      <mesh ref={meshRef}>
        <boxGeometry args={[1.15, 1.15, 1.15]} />
        <meshStandardMaterial
          color="#0088ff"
          metalness={0.85}
          roughness={0.22}
          emissive="#00d4ff"
          emissiveIntensity={0.45}
        />
      </mesh>
      <mesh scale={1.08}>
        <boxGeometry args={[1.15, 1.15, 1.15]} />
        <meshBasicMaterial color="#00d4ff" wireframe transparent opacity={0.35} />
      </mesh>
    </Float>
  );
}

/** Three.js rotating metallic cube with cyan glow for the login logo */
const CryptoCube = () => {
  return (
    <div className="crypto-cube-wrap" aria-hidden="true">
      <div className="crypto-cube-aura" />
      <Canvas
        dpr={[1, 1.5]}
        camera={{ position: [0, 0, 3.2], fov: 42 }}
        gl={{ antialias: true, alpha: true }}
        style={{ width: 80, height: 80 }}
      >
        <ambientLight intensity={0.55} />
        <pointLight position={[3, 3, 4]} intensity={1.4} color="#00d4ff" />
        <pointLight position={[-3, -2, -2]} intensity={0.6} color="#0088ff" />
        <Suspense fallback={null}>
          <GlowingCube />
        </Suspense>
      </Canvas>
    </div>
  );
};

export default CryptoCube;
