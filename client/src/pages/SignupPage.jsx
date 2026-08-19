import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Loader2, Check } from 'lucide-react';
import OrbitingCoins from '../components/OrbitingCoins';
import CryptoCube from '../components/CryptoCube';

const SignupPage = () => {
  const navigate = useNavigate();
  const [status, setStatus] = useState('idle');
  const [form, setForm] = useState({ name: '', email: '', password: '' });

  const onChange = (e) => setForm((f) => ({ ...f, [e.target.name]: e.target.value }));

  // Backend disabled for now — plays the success animation and enters.
  const handleSubmit = (e) => {
    e.preventDefault();
    if (status === 'loading') return;
    setStatus('loading');
    window.setTimeout(() => {
      setStatus('success');
      window.setTimeout(() => navigate('/dashboard', { replace: true }), 700);
    }, 900);
  };

  return (
    <div className="login-page relative flex min-h-screen items-center justify-center overflow-hidden px-5 py-10">
      <OrbitingCoins />

      <div className="login-card relative z-10 w-full max-w-[420px]">
        <div className="login-card-inner login-card-cinematic">
          <CryptoCube />
          <h1 className="login-title">CipherChain</h1>
          <p className="login-subtitle">Create your secure investigation account</p>

          <form onSubmit={handleSubmit} className="mt-8 space-y-5">
            <input
              name="name"
              type="text"
              value={form.name}
              onChange={onChange}
              placeholder="Full name"
              className="login-input"
              required
            />
            <input
              name="email"
              type="email"
              value={form.email}
              onChange={onChange}
              placeholder="Email address"
              className="login-input"
              required
            />
            <input
              name="password"
              type="password"
              value={form.password}
              onChange={onChange}
              placeholder="Password (min 8 characters)"
              className="login-input"
              required
              minLength={8}
            />

            {error && (
              <div className="flex items-start gap-2 rounded-lg border border-red-500/40 bg-red-950/40 px-3 py-2 text-sm text-red-300">
                <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0" />
                <span>{error}</span>
              </div>
            )}

            <button
              type="submit"
              disabled={status !== 'idle'}
              className={`login-btn ${status === 'success' ? 'login-btn-success' : ''}`}
            >
              {status === 'loading' && (
                <span className="flex items-center justify-center gap-2">
                  <Loader2 className="h-5 w-5 animate-spin" /> CREATING…
                </span>
              )}
              {status === 'success' && (
                <span className="flex items-center justify-center gap-2">
                  <Check className="h-5 w-5" /> ACCOUNT READY
                </span>
              )}
              {status === 'idle' && 'CREATE ACCOUNT'}
            </button>
          </form>

          <p className="login-footer">
            Already have an account?{' '}
            <Link to="/login" className="login-signup-link">Sign In</Link>
          </p>
        </div>
      </div>
    </div>
  );
};

export default SignupPage;
