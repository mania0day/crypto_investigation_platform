import React, { useState } from 'react';
import { Search, Loader2 } from 'lucide-react';

const SearchBar = ({ onSearch, isLoading, selectedChain, compact = false }) => {
  const [query, setQuery] = useState('');

  const handleSubmit = (e) => {
    e.preventDefault();
    if (query.trim()) {
      onSearch(query.trim());
    }
  };

  const getPlaceholder = () => {
    switch (selectedChain) {
      case 'bitcoin': return 'BTC address, tx/block hash, or height…';
      case 'ethereum': return 'ETH address, 0x tx hash, or block #…';
      case 'tron': return 'TRX address, tx hash, or block #…';
      default: return 'Address, hash, or block…';
    }
  };

  return (
    <form onSubmit={handleSubmit} className={`relative w-full ${compact ? '' : 'mx-auto mb-10 max-w-3xl'}`}>
      <div className="relative group">
        <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-4">
          <Search className="h-4 w-4 text-vaultrix-textMuted group-focus-within:text-vaultrix-cyan transition-colors" />
        </div>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className={`glass-input w-full rounded-xl border border-vaultrix-border bg-vaultrix-card/70 pl-11 pr-28 text-vaultrix-text shadow-lg backdrop-blur-md transition-all duration-300 focus:border-vaultrix-cyan focus:shadow-[0_0_25px_rgba(0,240,255,0.18)] ${
            compact ? 'py-2.5 text-sm' : 'py-4 text-lg'
          }`}
          placeholder={getPlaceholder()}
        />
        <div className={`absolute inset-y-1.5 right-1.5`}>
          <button
            type="submit"
            disabled={isLoading || !query.trim()}
            className={`flex h-full items-center justify-center rounded-lg bg-vaultrix-cyan font-bold uppercase tracking-widest text-black transition-all hover:bg-vaultrix-cyanHover disabled:cursor-not-allowed disabled:opacity-50 ${
              compact ? 'px-4 text-xs' : 'btn-cyan px-6 text-sm'
            }`}
          >
            {isLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Search'}
          </button>
        </div>
      </div>
    </form>
  );
};

export default SearchBar;
