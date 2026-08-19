import React, { useCallback, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { Eye, EyeOff, Loader2, Check } from 'lucide-react';
import OrbitingCoins from '../components/OrbitingCoins';
import CryptoCube from '../components/CryptoCube';

const LoginPage = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const cardRef = useRef(null);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [status, setStatus] = useState('idle');
  const [tilt, setTilt] = useState({ x: 0, y: 0 });

  const redirectTo = location.state?.from || '/dashboard';

  // Backend disabled for now — this just plays the success animation and enters.
  const handleSubmit = (e) => {
    e.preventDefault();
    if (!email.trim() || !password.trim() || status === 'loading') return;

    setStatus('loading');
    window.setTimeout(() => {
      setStatus('success');
      window.setTimeout(() => navigate(redirectTo, { replace: true }), 700);
    }, 900);
  };

  const onCardMove = useCallback((e) => {
    const el = cardRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width;
    const py = (e.clientY - rect.top) / rect.height;
    setTilt({
      x: (0.5 - py) * 8,
      y: (px - 0.5) * 10
    });
  }, []);

  const onCardLeave = useCallback(() => {
    setTilt({ x: 0, y: 0 });
  }, []);

  return (
    <div className="login-page relative flex min-h-screen items-center justify-center overflow-hidden px-5 py-10">
      <OrbitingCoins />

      <div
        className="login-card relative z-10 w-full max-w-[420px]"
        style={{ perspective: '1000px' }}
      >
        <div
          ref={cardRef}
          className="login-card-inner login-card-cinematic login-card-tilt"
          onMouseMove={onCardMove}
          onMouseLeave={onCardLeave}
          style={{
            transform: `rotateX(${tilt.x}deg) rotateY(${tilt.y}deg)`,
            transition: tilt.x === 0 && tilt.y === 0 ? 'transform 0.5s ease' : 'transform 0.08s ease-out'
          }}
        >
          <CryptoCube />

          <h1 className="login-title">CipherChain</h1>
          <p className="login-subtitle">Professional crypto analysis &amp; on-chain investigation</p>

          <form onSubmit={handleSubmit} className="mt-8 space-y-5">
            <div>
              <label htmlFor="login-email" className="sr-only">Email</label>
              <input
                id="login-email"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="Email address"
                className="login-input"
                required
              />
            </div>

            <div>
              <label htmlFor="login-password" className="sr-only">Password</label>
              <div className="relative">
                <input
                  id="login-password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Password"
                  className="login-input pr-12"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  className="absolute inset-y-0 right-0 flex items-center px-3 text-white/40 transition hover:text-[#00d4ff]"
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                >
                  {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
              <div className="mt-2 flex justify-end">
                <button type="button" className="login-forgot">
                  Forgot Password?
                </button>
              </div>
            </div>

            <button
              type="submit"
              disabled={status === 'loading' || status === 'success'}
              className={`login-btn ${status === 'success' ? 'login-btn-success' : ''}`}
            >
              {status === 'loading' && (
                <span className="flex items-center justify-center gap-2">
                  <Loader2 className="h-5 w-5 animate-spin" />
                  AUTHENTICATING…
                </span>
              )}
              {status === 'success' && (
                <span className="flex items-center justify-center gap-2">
                  <Check className="h-5 w-5" />
                  ACCESS GRANTED
                </span>
              )}
              {status === 'idle' && 'ENTER SYSTEM'}
            </button>
          </form>

          <p className="login-footer">
            Don&apos;t have an account?{' '}
            <Link to="/signup" className="login-signup-link">Sign Up</Link>
          </p>
        </div>
      </div>
    </div>
  );
};

export default LoginPage;
