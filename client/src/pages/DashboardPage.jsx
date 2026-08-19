import React, { useState } from 'react';
import axios from 'axios';
import { AlertTriangle, ArrowLeft, ExternalLink, Loader2, Radar } from 'lucide-react';
import { Link } from 'react-router-dom';
import ResultCard from '../components/ResultCard';
import SearchBar from '../components/SearchBar';
import TxFlowGraph from '../components/TxFlowGraph';
import FundFlowHero from '../components/FundFlowHero';
import AnimatedNetworkBackground from '../components/AnimatedNetworkBackground';
import { validateAndDetect } from '../utils/detectQuery';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:4000/api';
// The investigation backend serves its own UI. Configurable because it is a
// separate deployment: it needs Postgres and minutes-long runs, which the
// serverless host this dashboard sits on cannot give it.
const INVESTIGATE_URL = import.meta.env.VITE_INVESTIGATE_URL || 'http://localhost:8000/';

const DashboardPage = () => {
  const [selectedChain] = useState('bitcoin');
  const [isLoading, setIsLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [showSearch, setShowSearch] = useState(false);

  const fetchEndpoint = async (chain, type, query) => {
    const url = `${API_BASE}/${chain}/${type}/${encodeURIComponent(query)}`;
    return axios.get(url);
  };

  const handleSearch = async (rawQuery) => {
    setIsLoading(true);
    setError(null);
    setResult(null);

    const detected = validateAndDetect(rawQuery, selectedChain);
    if (detected.error) {
      setError(detected.error);
      setIsLoading(false);
      return;
    }

    try {
      const response = await fetchEndpoint(selectedChain, detected.type, detected.query);
      setResult(response.data);
    } catch (err) {
      if (detected.alsoTryBlock) {
        try {
          const blockRes = await fetchEndpoint(selectedChain, 'block', detected.query);
          setResult(blockRes.data);
          setIsLoading(false);
          return;
        } catch {
          // fall through
        }
      }
      console.error(err);
      setError(err.response?.data?.error || 'Lookup failed. Check the value and selected chain.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleNodeClick = (node) => {
    if (!node?.fullId || node.fullId === 'Coinbase') return;
    if (node.kind === 'block') return;
    handleSearch(node.fullId);
  };

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
            {/* Hands off to the investigation backend, which serves its own UI.
                A separate origin on purpose: a trace runs for minutes against a
                database, which is the opposite of what this dashboard's
                serverless lookup is built for. */}
            <a
              href={INVESTIGATE_URL}
              target="_blank"
              rel="noreferrer"
              className="group flex items-center gap-2 rounded-lg border border-vaultrix-cyan/40 bg-vaultrix-cyan/10 px-3 py-1.5 text-vaultrix-cyan transition-colors hover:border-vaultrix-cyan hover:bg-vaultrix-cyan/20"
              title="Trace an address to the nearest exchange, with evidence"
            >
              <Radar className="h-4 w-4" />
              <span className="font-display text-xs font-bold uppercase tracking-widest">
                Investigate
              </span>
              <ExternalLink className="h-3 w-3 opacity-60 transition-opacity group-hover:opacity-100" />
            </a>

            <div className="font-mono text-xs text-vaultrix-textMuted">
              STATUS: <span className="text-green-400">ONLINE</span>
            </div>
          </div>
        </header>

        {error && (
          <div className="mx-6 mt-4 flex items-start rounded-lg border border-red-500/30 bg-red-950/40 p-4 text-red-300">
            <AlertTriangle className="mr-3 h-5 w-5 flex-shrink-0" />
            <div>
              <h3 className="font-semibold">Lookup Failed</h3>
              <p className="text-sm opacity-90">{error}</p>
            </div>
          </div>
        )}

        <div className="flex-1 px-4 py-4 sm:px-6">
          {isLoading && (
            <div className="flex min-h-[320px] flex-col items-center justify-center gap-3">
              <Loader2 className="h-12 w-12 animate-spin text-vaultrix-cyan" />
              <p className="font-display text-sm tracking-widest text-vaultrix-cyan">MAPPING FLOW…</p>
            </div>
          )}

          {!result && !isLoading && (
            <div className="w-full">
              {showSearch && (
                <div className="mx-auto mb-4 w-full max-w-3xl">
                  <SearchBar onSearch={handleSearch} isLoading={isLoading} selectedChain={selectedChain} compact />
                </div>
              )}
              <FundFlowHero onWalletIntelligence={() => setShowSearch((s) => !s)} />
              <p className="mt-4 text-center text-sm text-vaultrix-textMuted">
                Open Wallet Intelligence to trace an address — the graph and full technical details
                appear here.
              </p>
            </div>
          )}

          {result && !isLoading && (
            <div className="mx-auto flex w-full max-w-7xl flex-col gap-6">
              {/* Graph canvas */}
              <div className="min-h-[420px] overflow-hidden rounded-xl border border-vaultrix-border bg-vaultrix-bg/40">
                {result.graph ? (
                  <TxFlowGraph graph={result.graph} onNodeClick={handleNodeClick} />
                ) : (
                  <div className="flex h-[420px] items-center justify-center text-vaultrix-textMuted">
                    No flow graph for this result — details are below.
                  </div>
                )}
              </div>

              {/* Full details: Block ID + Technical Details always here */}
              <ResultCard result={result} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default DashboardPage;
