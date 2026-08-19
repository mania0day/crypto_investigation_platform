import React from 'react';
import { Bitcoin, Layers, Hexagon } from 'lucide-react';

const chains = [
  { id: 'bitcoin', name: 'Bitcoin', icon: Bitcoin, color: 'text-amber-400' },
  { id: 'ethereum', name: 'Ethereum', icon: Layers, color: 'text-sky-400' },
  { id: 'tron', name: 'Tron', icon: Hexagon, color: 'text-red-400' }
];

const ChainTabs = ({ selectedChain, onSelectChain }) => {
  return (
    <div className="flex space-x-2 overflow-x-auto pb-1">
      {chains.map((chain) => {
        const Icon = chain.icon;
        const isActive = selectedChain === chain.id;

        return (
          <button
            key={chain.id}
            onClick={() => onSelectChain(chain.id)}
            className={`flex items-center space-x-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-300 ${
              isActive
                ? 'border border-vaultrix-cyan/50 bg-vaultrix-card text-white shadow-[0_0_15px_rgba(0,240,255,0.18)]'
                : 'border border-vaultrix-border bg-vaultrix-bg/50 text-vaultrix-textMuted hover:bg-vaultrix-card hover:text-white'
            }`}
          >
            <Icon className={`h-4 w-4 ${isActive ? chain.color : ''}`} />
            <span>{chain.name}</span>
          </button>
        );
      })}
    </div>
  );
};

export default ChainTabs;
