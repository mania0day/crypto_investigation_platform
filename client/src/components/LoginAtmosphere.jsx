import React, { useEffect, useRef } from 'react';

/**
 * Animated deep-space login background:
 * flowing gradients, particles, glow lines, floating crypto glyphs.
 */
const LoginAtmosphere = () => {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    let raf = 0;
    let w = 0;
    let h = 0;
    let t = 0;

    const particles = [];
    const glyphs = ['₿', 'Ξ', '◎', '◈', '⚡'];
    const floaters = [];

    const resize = () => {
      w = canvas.width = window.innerWidth;
      h = canvas.height = window.innerHeight;
      particles.length = 0;
      const count = Math.min(70, Math.floor((w * h) / 22000));
      for (let i = 0; i < count; i++) {
        particles.push({
          x: Math.random() * w,
          y: Math.random() * h,
          r: Math.random() * 1.6 + 0.4,
          vx: (Math.random() - 0.5) * 0.25,
          vy: (Math.random() - 0.5) * 0.25,
          a: Math.random() * 0.5 + 0.2
        });
      }
      floaters.length = 0;
      for (let i = 0; i < 8; i++) {
        floaters.push({
          g: glyphs[i % glyphs.length],
          x: Math.random() * w,
          y: Math.random() * h,
          s: 14 + Math.random() * 18,
          phase: Math.random() * Math.PI * 2,
          speed: 0.003 + Math.random() * 0.004
        });
      }
    };

    const draw = () => {
      t += 0.008;
      ctx.clearRect(0, 0, w, h);

      // Flowing radial washes: blue → purple → dark
      const g1 = ctx.createRadialGradient(
        w * (0.25 + Math.sin(t * 0.4) * 0.1),
        h * (0.3 + Math.cos(t * 0.35) * 0.08),
        0,
        w * 0.3,
        h * 0.35,
        w * 0.65
      );
      g1.addColorStop(0, 'rgba(0, 136, 255, 0.18)');
      g1.addColorStop(0.45, 'rgba(45, 0, 71, 0.22)');
      g1.addColorStop(1, 'rgba(10, 10, 26, 0)');
      ctx.fillStyle = g1;
      ctx.fillRect(0, 0, w, h);

      const g2 = ctx.createRadialGradient(
        w * (0.78 + Math.cos(t * 0.5) * 0.08),
        h * (0.7 + Math.sin(t * 0.45) * 0.07),
        0,
        w * 0.75,
        h * 0.7,
        w * 0.5
      );
      g2.addColorStop(0, 'rgba(0, 212, 255, 0.12)');
      g2.addColorStop(0.5, 'rgba(26, 0, 51, 0.2)');
      g2.addColorStop(1, 'rgba(10, 10, 26, 0)');
      ctx.fillStyle = g2;
      ctx.fillRect(0, 0, w, h);

      // Soft wave bands
      ctx.beginPath();
      for (let x = 0; x <= w; x += 8) {
        const y = h * 0.55 + Math.sin(x * 0.008 + t) * 28 + Math.sin(x * 0.003 + t * 0.7) * 18;
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.strokeStyle = 'rgba(0, 136, 255, 0.12)';
      ctx.lineWidth = 2;
      ctx.stroke();

      // Occasional glowing horizontal scan lines
      const lineY = ((t * 40) % (h + 80)) - 40;
      const lg = ctx.createLinearGradient(0, lineY, w, lineY);
      lg.addColorStop(0, 'rgba(0,212,255,0)');
      lg.addColorStop(0.5, 'rgba(0,212,255,0.18)');
      lg.addColorStop(1, 'rgba(0,212,255,0)');
      ctx.strokeStyle = lg;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, lineY);
      ctx.lineTo(w, lineY);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(w * 0.2, lineY, 2.5, 0, Math.PI * 2);
      ctx.arc(w * 0.8, lineY, 2.5, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(0,212,255,0.7)';
      ctx.fill();

      // Particles
      for (const p of particles) {
        p.x += p.vx;
        p.y += p.vy;
        if (p.x < 0 || p.x > w) p.vx *= -1;
        if (p.y < 0 || p.y > h) p.vy *= -1;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(0, 212, 255, ${p.a})`;
        ctx.shadowColor = '#00d4ff';
        ctx.shadowBlur = 8;
        ctx.fill();
        ctx.shadowBlur = 0;
      }

      // Floating crypto glyphs
      for (const f of floaters) {
        const alpha = 0.08 + Math.sin(t * 1.2 + f.phase) * 0.07;
        f.y -= f.speed * 8;
        f.x += Math.sin(t + f.phase) * 0.15;
        if (f.y < -40) {
          f.y = h + 40;
          f.x = Math.random() * w;
        }
        ctx.font = `${f.s}px Inter, sans-serif`;
        ctx.fillStyle = `rgba(0, 212, 255, ${alpha})`;
        ctx.fillText(f.g, f.x, f.y);
      }

      raf = requestAnimationFrame(draw);
    };

    resize();
    draw();
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      cancelAnimationFrame(raf);
    };
  }, []);

  return (
    <>
      <div className="login-gradient-bg" aria-hidden="true" />
      <canvas ref={canvasRef} className="pointer-events-none fixed inset-0 z-0" aria-hidden="true" />
    </>
  );
};

export default LoginAtmosphere;
