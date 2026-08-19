import React from 'react';
import { useNavigate } from 'react-router-dom';
import { Shield } from 'lucide-react';
import AnimatedNetworkBackground from '../components/AnimatedNetworkBackground';

const LandingPage = () => {
  const navigate = useNavigate();

  return (
    <div className="relative h-screen w-full overflow-hidden bg-transparent">
      <AnimatedNetworkBackground intensity={0.55} />
      <div className="absolute inset-0 bg-gradient-to-b from-[#02050c]/50 via-transparent to-[#02050c]/70" />

      <div className="relative z-10 flex h-full w-full flex-col px-8 pt-8 sm:px-16">
        <nav className="mb-auto flex w-full items-center justify-between">
          <div className="flex items-center space-x-2">
            <Shield className="h-8 w-8 text-vaultrix-cyan" />
            <span className="font-display text-2xl font-bold uppercase tracking-widest text-vaultrix-cyan">
              CipherChain
            </span>
          </div>
        </nav>

        <div className="relative mx-auto w-full max-w-4xl animate-in fade-in zoom-in px-4 py-8 duration-1000">
          <div className="flex w-full flex-col items-center justify-center space-y-8 text-center">
            <h1 className="font-display text-5xl font-black uppercase tracking-widest text-white drop-shadow-[0_0_15px_rgba(0,240,255,0.4)] md:text-6xl lg:text-7xl">
              <span className="text-vaultrix-cyan">CipherChain //</span> Decrypt<br /> The Blockchain
            </h1>

            <p className="mx-auto max-w-3xl text-lg leading-relaxed text-gray-300 md:text-xl">
              Trace wallets as living node graphs. Follow value flows across Bitcoin, Ethereum, and Tron with an animated investigation canvas.
            </p>

            <div className="flex flex-wrap items-center justify-center gap-4 pt-8">
              <button
                onClick={() => navigate('/login')}
                className="btn-cyan btn-hero-primary px-12 py-4 text-lg"
              >
                [ ENTER SYSTEM <span className="btn-hero-arrow">&rarr;</span> ]
              </button>
              <button
                onClick={() => navigate('/dashboard')}
                className="btn-hero-secondary rounded-sm border border-vaultrix-border px-8 py-4 text-sm font-bold uppercase tracking-widest text-vaultrix-textMuted transition hover:border-vaultrix-cyan hover:text-vaultrix-cyan"
              >
                Skip to Graph
              </button>
            </div>
          </div>
        </div>

        <div className="mt-auto pb-8" />
      </div>
    </div>
  );
};

export default LandingPage;
