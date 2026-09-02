import React from 'react';
import { ArrowLeft, Radar } from 'lucide-react';
import { Link } from 'react-router-dom';
import FundFlowHero from '../components/FundFlowHero';
import AnimatedNetworkBackground from '../components/AnimatedNetworkBackground';

// The investigation backend serves its own UI. Configurable because it is a
// separate deployment: it needs Postgres and minutes-long runs, which the
// serverless host this dashboard sits on cannot give it.
const INVESTIGATE_URL = import.meta.env.VITE_INVESTIGATE_URL || 'http://localhost:8000/';

function investigateHref() {
  try {
    const url = new URL(INVESTIGATE_URL, window.location.origin);
    url.searchParams.set('return', `${window.location.origin}/dashboard`);
    return url.toString();
  } catch {
    return INVESTIGATE_URL;
  }
}

const DashboardPage = () => {
  return (
    <div className="relative min-h-screen w-full overflow-x-hidden bg-transparent">
      <AnimatedNetworkBackground intensity={0.45} />

      <div className="relative z-10 flex min-h-screen flex-col">
        <header className="flex items-center justify-between gap-4 border-b border-vaultrix-border bg-vaultrix-bg/70 px-6 py-3 backdrop-blur-xl">
          <Link to="/" className="flex items-center text-vaultrix-cyan transition-colors hover:text-vaultrix-cyanHover">
            <ArrowLeft className="mr-2 h-4 w-4" />
            <span className="font-display text-lg font-bold uppercase tracking-widest">CipherChain</span>
          </Link>

          <div className="flex items-center gap-4">
            {/* Same tab so browser Back and the engine's back link both return
                here. A blank tab has no dashboard history, and noreferrer hid
                the origin the engine uses to build that link. */}
            <a
              href={investigateHref()}
              className="group flex items-center gap-2 rounded-lg border border-vaultrix-cyan/40 bg-vaultrix-cyan/10 px-3 py-1.5 text-vaultrix-cyan transition-colors hover:border-vaultrix-cyan hover:bg-vaultrix-cyan/20"
              title="Trace an address to the nearest exchange, with evidence"
            >
              <Radar className="h-4 w-4" />
              <span className="font-display text-xs font-bold uppercase tracking-widest">
                Investigate
              </span>
            </a>

            <div className="font-mono text-xs text-vaultrix-textMuted">
              STATUS: <span className="text-green-400">ONLINE</span>
            </div>
          </div>
        </header>

        <div className="flex-1 px-4 py-4 sm:px-6">
          <FundFlowHero />
        </div>
      </div>
    </div>
  );
};

export default DashboardPage;
