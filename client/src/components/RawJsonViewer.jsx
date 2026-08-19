import React, { useState } from 'react';
import { ChevronDown, ChevronUp, Code, Copy, CheckCircle2 } from 'lucide-react';

const RawJsonViewer = ({ data }) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(JSON.stringify(data, null, 2));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="mt-6 overflow-hidden rounded-xl border border-vaultrix-border bg-vaultrix-bg/80">
      <div
        className="flex cursor-pointer items-center justify-between p-4 transition-colors hover:bg-vaultrix-card"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <div className="flex items-center space-x-2 text-vaultrix-textMuted">
          <Code className="h-5 w-5 text-vaultrix-cyan" />
          <span className="font-semibold text-vaultrix-text">Raw JSON Response</span>
        </div>
        {isExpanded ? (
          <ChevronUp className="h-5 w-5 text-vaultrix-textMuted" />
        ) : (
          <ChevronDown className="h-5 w-5 text-vaultrix-textMuted" />
        )}
      </div>

      {isExpanded && (
        <div className="relative border-t border-vaultrix-border">
          <button
            onClick={handleCopy}
            className="absolute right-4 top-4 rounded-lg border border-vaultrix-border bg-vaultrix-card p-2 text-vaultrix-textMuted shadow-md transition-colors hover:bg-vaultrix-cardHover hover:text-vaultrix-text"
            title="Copy JSON"
          >
            {copied ? <CheckCircle2 className="h-4 w-4 text-green-400" /> : <Copy className="h-4 w-4" />}
          </button>
          <pre className="max-h-96 overflow-x-auto p-4 text-sm text-sky-200">
            <code>{JSON.stringify(data, null, 2)}</code>
          </pre>
        </div>
      )}
    </div>
  );
};

export default RawJsonViewer;
