import React from 'react';
import { Hash, Clock, ArrowRightLeft, Coins, CheckCircle, XCircle, Box, FileCode, Wallet } from 'lucide-react';

const SectionLabel = ({ children, icon: Icon }) => (
  <p className="mb-3 flex items-center text-xs font-semibold uppercase tracking-[0.14em] text-vaultrix-textMuted">
    {Icon && <Icon className="mr-2 h-3.5 w-3.5 shrink-0 text-vaultrix-cyan" />}
    {children}
  </p>
);

/** Long values (hashes, addresses) */
const InfoBox = ({ label, children, icon: Icon }) => (
  <div className="flex h-full flex-col">
    <SectionLabel icon={Icon}>{label}</SectionLabel>
    <div className="flex min-h-[52px] flex-1 items-center break-all rounded-xl border border-vaultrix-border bg-[#060d18] px-4 py-3.5 font-mono text-[12px] leading-relaxed text-slate-100">
      {children === null || children === undefined || children === '' ? '—' : children}
    </div>
  </div>
);

/** Metric / technical detail card */
const DetailCard = ({ label, value, accent = false }) => (
  <div className="flex min-h-[96px] flex-col justify-between rounded-xl border border-vaultrix-border bg-[#0a1424] px-5 py-4 transition-colors hover:border-vaultrix-cyan/35">
    <p className="mb-3 text-[11px] font-medium uppercase tracking-[0.12em] text-vaultrix-textMuted">
      {label}
    </p>
    <p className={`break-words text-[15px] font-semibold leading-snug ${accent ? 'text-vaultrix-cyan' : 'text-white'}`}>
      {value ?? '—'}
    </p>
  </div>
);

const TechnicalDetailsSection = ({ items }) => {
  if (!items?.length) return null;
  return (
    <section className="rounded-2xl border border-vaultrix-border/80 bg-[#07101c]/70 p-5 sm:p-6 lg:p-8">
      <div className="mb-5 flex items-center gap-2 border-b border-vaultrix-border pb-4">
        <FileCode className="h-4 w-4 text-vaultrix-cyan" />
        <h3 className="text-sm font-bold uppercase tracking-[0.16em] text-vaultrix-cyan">
          Technical Details
        </h3>
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {items.map((item) => (
          <DetailCard key={item.label} label={item.label} value={item.value} />
        ))}
      </div>
    </section>
  );
};

function getTxTechItems(result) {
  if (result.chain === 'bitcoin') {
    return [
      { label: 'Size', value: result.size != null ? `${result.size} B` : '—' },
      { label: 'Virtual Size', value: result.virtualSize != null ? `${result.virtualSize} vB` : '—' },
      { label: 'Weight', value: result.weight != null ? `${result.weight} WU` : '—' },
      { label: 'Version', value: result.version ?? '—' },
      { label: 'Lock Time', value: result.lockTime ?? '—' },
      { label: 'RBF Enabled', value: result.rbfEnabled ? 'Yes' : 'No' },
      { label: 'Witness Data', value: result.hasWitness ? 'Yes' : 'No' },
      { label: 'Coinbase Tx', value: result.isCoinbaseTx ? 'Yes' : 'No' },
      ...(result.opReturnData ? [{ label: 'OP_RETURN', value: result.opReturnData }] : [])
    ];
  }
  if (result.chain === 'ethereum') {
    return [
      { label: 'Nonce', value: result.nonce ?? '—' },
      { label: 'Tx Index', value: result.transactionIndex ?? '—' },
      { label: 'Gas Limit', value: result.gasLimit != null ? Number(result.gasLimit).toLocaleString() : '—' },
      { label: 'Gas Used', value: result.gasUsed != null ? Number(result.gasUsed).toLocaleString() : '—' },
      { label: 'Gas Price', value: result.gasPriceGwei != null ? `${result.gasPriceGwei} Gwei` : '—' },
      { label: 'Tx Type', value: result.transactionType ?? '—' },
      { label: 'Contract Call', value: result.isContractInteraction ? 'Yes' : 'No' }
    ];
  }
  if (result.chain === 'tron') {
    return [
      { label: 'Contract Type', value: result.contractType || '—' },
      { label: 'Energy Usage', value: Number(result.energyUsage ?? 0).toLocaleString() },
      { label: 'Bandwidth Usage', value: Number(result.netUsage ?? 0).toLocaleString() },
      { label: 'Net Fee', value: result.netFeeTrx != null ? `${result.netFeeTrx} TRX` : '—' },
      { label: 'Energy Fee', value: result.energyFeeTrx != null ? `${result.energyFeeTrx} TRX` : '—' },
      {
        label: 'Expiration',
        value: result.expiration ? new Date(result.expiration).toLocaleString() : '—'
      }
    ];
  }
  return [];
}

function getBlockTechItems(result) {
  const items = [];
  if (result.chain === 'bitcoin') {
    items.push(
      { label: 'Size', value: result.size != null ? `${result.size} B` : '—' },
      { label: 'Weight', value: result.weight ?? '—' },
      { label: 'Difficulty', value: result.difficulty != null ? Number(result.difficulty).toLocaleString() : '—' },
      { label: 'Nonce', value: result.nonce ?? '—' },
      { label: 'Version', value: result.version ?? '—' },
      { label: 'Bits', value: result.bits ?? '—' }
    );
  }
  if (result.chain === 'ethereum') {
    items.push(
      { label: 'Gas Used', value: result.gasUsed != null ? Number(result.gasUsed).toLocaleString() : '—' },
      { label: 'Gas Limit', value: result.gasLimit != null ? Number(result.gasLimit).toLocaleString() : '—' },
      { label: 'Size', value: result.size != null ? `${result.size} B` : '—' },
      { label: 'Difficulty', value: result.difficulty != null ? Number(result.difficulty).toLocaleString() : '—' },
      { label: 'Nonce', value: result.nonce ?? '—' },
      ...(result.miner ? [{ label: 'Miner', value: result.miner }] : [])
    );
  }
  if (result.chain === 'tron') {
    items.push(
      { label: 'Version', value: result.version ?? '—' },
      ...(result.witnessAddress ? [{ label: 'Witness Address', value: result.witnessAddress }] : [])
    );
  }
  if (result.parentHash || result.previousBlockHash) {
    items.push({ label: 'Parent Block', value: result.parentHash || result.previousBlockHash });
  }
  if (result.merkleRoot) {
    items.push({ label: 'Merkle Root', value: result.merkleRoot });
  }
  return items;
}

const ResultCard = ({ result }) => {
  if (!result) return null;

  const isTx = result.type === 'transaction';
  const isBlock = result.type === 'block';
  const isAddress = result.type === 'address';
  const typeLabel = isTx ? 'Transaction' : isBlock ? 'Block' : 'Address';

  const blockIdValue = isBlock
    ? (result.hash || '—')
    : (result.blockId || result.blockHash || result.raw?.info?.blockHash || result.raw?.status?.block_hash || 'Not available');

  return (
    <article className="w-full overflow-hidden rounded-2xl border border-vaultrix-border bg-vaultrix-card/90 shadow-[0_20px_50px_rgba(0,0,0,0.35)] backdrop-blur-md">
      {/* Header */}
      <header className="flex items-start gap-4 border-b border-vaultrix-border px-6 py-5 sm:px-8 sm:py-6">
        <div className="rounded-xl border border-vaultrix-border bg-[#060d18] p-3">
          {isTx && <ArrowRightLeft className="h-5 w-5 text-vaultrix-cyan" />}
          {isBlock && <Box className="h-5 w-5 text-sky-400" />}
          {isAddress && <Wallet className="h-5 w-5 text-green-400" />}
        </div>
        <div className="min-w-0 flex-1 pt-0.5">
          <h2 className="text-lg font-bold uppercase tracking-[0.12em] text-white">
            {result.chain} {typeLabel}
          </h2>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-vaultrix-textMuted">
            {result.status === 'success' && (
              <span className="inline-flex items-center gap-1.5 text-green-400">
                <CheckCircle className="h-3.5 w-3.5" /> Success
              </span>
            )}
            {result.status === 'failed' && (
              <span className="inline-flex items-center gap-1.5 text-red-400">
                <XCircle className="h-3.5 w-3.5" /> Failed
              </span>
            )}
            {result.status === 'pending' && (
              <span className="inline-flex items-center gap-1.5 text-amber-400">
                <Clock className="h-3.5 w-3.5 animate-pulse" /> Pending
              </span>
            )}
            {result.timestamp && result.timestamp !== 'Unknown' && result.timestamp !== 'Unconfirmed' && (
              <span>{new Date(result.timestamp).toLocaleString()}</span>
            )}
            {result.confirmations !== undefined && (
              <span>{Number(result.confirmations).toLocaleString()} confirmations</span>
            )}
          </div>
        </div>
      </header>

      <div className="space-y-8 px-6 py-6 sm:px-8 sm:py-8">
        {isAddress && (
          <>
            <InfoBox label="Address" icon={Wallet}>{result.address}</InfoBox>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <DetailCard label="Balance" value={result.balance} accent />
              <DetailCard
                label="Balance (USD)"
                value={result.balanceUsd != null ? `$${Number(result.balanceUsd).toLocaleString()}` : '—'}
              />
              <DetailCard label="Tx Count" value={Number(result.txCount || 0).toLocaleString()} />
              {result.totalReceived && <DetailCard label="Total Received" value={result.totalReceived} />}
              {result.totalSent && <DetailCard label="Total Sent" value={result.totalSent} />}
            </div>
            {Array.isArray(result.recentTxs) && result.recentTxs.length > 0 && (
              <section>
                <SectionLabel icon={Hash}>Recent Transactions</SectionLabel>
                <div className="grid gap-4 sm:grid-cols-2">
                  {result.recentTxs.map((tx) => (
                    <div
                      key={tx.hash}
                      className="rounded-xl border border-vaultrix-border bg-[#0a1424] px-5 py-4"
                    >
                      <p className="break-all font-mono text-[11px] leading-relaxed text-vaultrix-cyan">{tx.hash}</p>
                      <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-vaultrix-textMuted">
                        {tx.blockNumber != null && (
                          <span className="rounded-md bg-[#060d18] px-2.5 py-1">Block {tx.blockNumber}</span>
                        )}
                        {tx.value && (
                          <span className="rounded-md bg-[#060d18] px-2.5 py-1">{tx.value}</span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}
          </>
        )}

        {isTx && (
          <>
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2 lg:gap-8">
              <div className="space-y-5">
                <InfoBox label="Transaction Hash" icon={Hash}>{result.hash}</InfoBox>
                <InfoBox label="Block Number" icon={Box}>{result.blockNumber ?? 'Pending / Unknown'}</InfoBox>
                <InfoBox label="Block ID" icon={Hash}>{blockIdValue}</InfoBox>
              </div>
              <div className="space-y-5">
                <InfoBox label="From">{result.from}</InfoBox>
                <InfoBox label="To">{result.to}</InfoBox>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <DetailCard label="Value" value={result.value} accent />
                  <DetailCard
                    label="Value (USD)"
                    value={result.valueUsd != null ? `$${Number(result.valueUsd).toLocaleString()}` : '—'}
                  />
                  {result.fee !== undefined && (
                    <>
                      <DetailCard label="Network Fee" value={result.fee} />
                      <DetailCard
                        label="Fee (USD)"
                        value={result.feeUsd != null ? `$${Number(result.feeUsd).toLocaleString()}` : '—'}
                      />
                    </>
                  )}
                </div>
              </div>
            </div>

            <TechnicalDetailsSection items={getTxTechItems(result)} />
          </>
        )}

        {isBlock && (
          <>
            <div className="grid grid-cols-1 gap-5 lg:grid-cols-2 lg:gap-6">
              <InfoBox label="Block ID" icon={Hash}>{blockIdValue}</InfoBox>
              <InfoBox label="Block Number / Height" icon={Box}>{result.blockNumber}</InfoBox>
              <InfoBox label="Timestamp" icon={Clock}>
                {result.timestamp ? new Date(result.timestamp).toLocaleString() : 'Unknown'}
              </InfoBox>
              <InfoBox label="Transactions in Block" icon={ArrowRightLeft}>
                {Number(result.txCount || 0).toLocaleString()}
              </InfoBox>
            </div>

            <TechnicalDetailsSection items={getBlockTechItems(result)} />

            {Array.isArray(result.sampleTxs) && result.sampleTxs.length > 0 && (
              <section>
                <SectionLabel icon={Hash}>Sample Transactions</SectionLabel>
                <div className="grid gap-4 sm:grid-cols-2">
                  {result.sampleTxs.map((tx) => (
                    <div
                      key={tx.hash}
                      className="rounded-xl border border-vaultrix-border bg-[#0a1424] px-5 py-4"
                    >
                      <p className="break-all font-mono text-[11px] leading-relaxed text-vaultrix-cyan">{tx.hash}</p>
                      {tx.value && <p className="mt-3 text-xs text-vaultrix-textMuted">{tx.value}</p>}
                    </div>
                  ))}
                </div>
              </section>
            )}
          </>
        )}
      </div>
    </article>
  );
};

export default ResultCard;
