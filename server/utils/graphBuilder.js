/**
 * Build a MetaSleuth-style graph from Blockstream-style vin/vout txs.
 * Center node = queried address or tx; edges = value flows.
 */

function shortId(value, len = 8) {
  if (!value) return 'unknown';
  if (value.length <= len * 2) return value;
  return `${value.slice(0, len)}…${value.slice(-4)}`;
}

function satsToBtc(sats) {
  return (Number(sats || 0) / 1e8);
}

function buildAddressGraph(centerAddress, txs = [], btcPrice = 0) {
  const nodesMap = new Map();
  const links = [];

  nodesMap.set(centerAddress, {
    id: centerAddress,
    label: shortId(centerAddress, 6),
    fullId: centerAddress,
    kind: 'address',
    role: 'center',
    val: 18
  });

  let linkIndex = 0;

  for (const tx of txs.slice(0, 12)) {
    const txid = tx.txid;
    const timestamp = tx.status?.block_time
      ? new Date(tx.status.block_time * 1000).toISOString()
      : null;

    const inputs = (tx.vin || [])
      .map((vin) => ({
        address: vin.prevout?.scriptpubkey_address || (vin.is_coinbase ? 'Coinbase' : null),
        value: vin.prevout?.value || 0
      }))
      .filter((x) => x.address);

    const outputs = (tx.vout || [])
      .map((vout) => ({
        address: vout.scriptpubkey_address || null,
        value: vout.value || 0
      }))
      .filter((x) => x.address);

    const centerIsInput = inputs.some((i) => i.address === centerAddress);
    const centerIsOutput = outputs.some((o) => o.address === centerAddress);

    // Incoming: other → center
    if (centerIsOutput) {
      const received = outputs
        .filter((o) => o.address === centerAddress)
        .reduce((s, o) => s + o.value, 0);

      const counterparts = inputs.filter((i) => i.address !== centerAddress);
      const sources = counterparts.length ? counterparts : [{ address: 'Unknown', value: received }];

      for (const src of sources.slice(0, 4)) {
        if (!nodesMap.has(src.address)) {
          nodesMap.set(src.address, {
            id: src.address,
            label: shortId(src.address, 6),
            fullId: src.address,
            kind: src.address === 'Coinbase' ? 'coinbase' : 'address',
            role: 'counterparty',
            val: 10
          });
        }

        const amountBtc = satsToBtc(src.value || received / sources.length);
        linkIndex += 1;
        links.push({
          id: `${txid}-in-${linkIndex}`,
          source: src.address,
          target: centerAddress,
          txHash: txid,
          direction: 'in',
          amountBtc,
          amountLabel: `${amountBtc.toFixed(8)} BTC`,
          amountUsd: (amountBtc * btcPrice).toFixed(2),
          timestamp,
          index: linkIndex
        });
      }
    }

    // Outgoing: center → other
    if (centerIsInput) {
      const counterparts = outputs.filter((o) => o.address !== centerAddress);
      for (const dest of counterparts.slice(0, 4)) {
        if (!nodesMap.has(dest.address)) {
          nodesMap.set(dest.address, {
            id: dest.address,
            label: shortId(dest.address, 6),
            fullId: dest.address,
            kind: 'address',
            role: 'counterparty',
            val: 10
          });
        }

        const amountBtc = satsToBtc(dest.value);
        linkIndex += 1;
        links.push({
          id: `${txid}-out-${linkIndex}`,
          source: centerAddress,
          target: dest.address,
          txHash: txid,
          direction: 'out',
          amountBtc,
          amountLabel: `${amountBtc.toFixed(8)} BTC`,
          amountUsd: (amountBtc * btcPrice).toFixed(2),
          timestamp,
          index: linkIndex
        });
      }
    }
  }

  return {
    nodes: Array.from(nodesMap.values()),
    links
  };
}

function buildTxGraph(tx, btcPrice = 0) {
  const nodesMap = new Map();
  const links = [];
  const txid = tx.txid || tx.hash;
  const timestamp = tx.status?.block_time
    ? new Date(tx.status.block_time * 1000).toISOString()
    : (tx.confirmed || null);

  nodesMap.set(txid, {
    id: txid,
    label: shortId(txid, 6),
    fullId: txid,
    kind: 'transaction',
    role: 'center',
    val: 20
  });

  const inputs = (tx.vin || []).map((vin, i) => ({
    address: vin.prevout?.scriptpubkey_address
      || (vin.is_coinbase ? 'Coinbase' : `input-${i}`),
    value: vin.prevout?.value || 0
  }));

  const outputs = (tx.vout || []).map((vout, i) => ({
    address: vout.scriptpubkey_address || `output-${i}`,
    value: vout.value || 0
  }));

  // BlockCypher-style fallback
  if (!tx.vin && tx.inputs) {
    inputs.push(
      ...tx.inputs.map((inp, i) => ({
        address: inp.addresses?.[0] || (inp.output_index === -1 ? 'Coinbase' : `input-${i}`),
        value: inp.output_value || 0
      }))
    );
  }
  if (!tx.vout && tx.outputs) {
    outputs.push(
      ...tx.outputs.map((out, i) => ({
        address: out.addresses?.[0] || `output-${i}`,
        value: out.value || 0
      }))
    );
  }

  let idx = 0;
  for (const inp of inputs.slice(0, 12)) {
    if (!nodesMap.has(inp.address)) {
      nodesMap.set(inp.address, {
        id: inp.address,
        label: shortId(inp.address, 6),
        fullId: inp.address,
        kind: inp.address === 'Coinbase' ? 'coinbase' : 'address',
        role: 'input',
        val: 11
      });
    }
    const amountBtc = satsToBtc(inp.value);
    idx += 1;
    links.push({
      id: `${txid}-in-${idx}`,
      source: inp.address,
      target: txid,
      txHash: txid,
      direction: 'in',
      amountBtc,
      amountLabel: `${amountBtc.toFixed(8)} BTC`,
      amountUsd: (amountBtc * btcPrice).toFixed(2),
      timestamp,
      index: idx
    });
  }

  for (const out of outputs.slice(0, 12)) {
    if (!nodesMap.has(out.address)) {
      nodesMap.set(out.address, {
        id: out.address,
        label: shortId(out.address, 6),
        fullId: out.address,
        kind: 'address',
        role: 'output',
        val: 11
      });
    }
    const amountBtc = satsToBtc(out.value);
    idx += 1;
    links.push({
      id: `${txid}-out-${idx}`,
      source: txid,
      target: out.address,
      txHash: txid,
      direction: 'out',
      amountBtc,
      amountLabel: `${amountBtc.toFixed(8)} BTC`,
      amountUsd: (amountBtc * btcPrice).toFixed(2),
      timestamp,
      index: idx
    });
  }

  return {
    nodes: Array.from(nodesMap.values()),
    links,
    inputs: inputs.map((i) => ({
      address: i.address,
      value: satsToBtc(i.value).toFixed(8) + ' BTC'
    })),
    outputs: outputs.map((o) => ({
      address: o.address,
      value: satsToBtc(o.value).toFixed(8) + ' BTC'
    }))
  };
}

function buildTransferGraph(centerId, transfers = [], options = {}) {
  const { unit = 'ETH', price = 0, kind = 'address' } = options;
  const nodesMap = new Map();
  const links = [];

  nodesMap.set(centerId, {
    id: centerId,
    label: shortId(centerId, 6),
    fullId: centerId,
    kind,
    role: 'center',
    val: 18
  });

  let index = 0;
  for (const t of transfers.slice(0, 16)) {
    const from = t.from || 'Unknown';
    const to = t.to || 'Unknown';
    const amount = Number(t.amount || 0);
    const counterparty = from.toLowerCase?.() === centerId.toLowerCase?.() ? to : from;
    const direction = from.toLowerCase?.() === centerId.toLowerCase?.() ? 'out' : 'in';

    if (!nodesMap.has(counterparty)) {
      nodesMap.set(counterparty, {
        id: counterparty,
        label: shortId(counterparty, 6),
        fullId: counterparty,
        kind: 'address',
        role: 'counterparty',
        val: 10
      });
    }

    index += 1;
    links.push({
      id: `${t.hash || index}-${direction}`,
      source: direction === 'out' ? centerId : counterparty,
      target: direction === 'out' ? counterparty : centerId,
      txHash: t.hash,
      direction,
      amountBtc: amount,
      amountLabel: `${amount.toFixed(6)} ${unit}`,
      amountUsd: (amount * price).toFixed(2),
      timestamp: t.timestamp || null,
      index
    });
  }

  return { nodes: Array.from(nodesMap.values()), links };
}

function buildSimpleTxGraph(hash, from, to, amount, unit, price, timestamp) {
  const nodes = [
    {
      id: from || 'from',
      label: shortId(from || 'from', 6),
      fullId: from,
      kind: 'address',
      role: 'input',
      val: 12
    },
    {
      id: hash,
      label: shortId(hash, 6),
      fullId: hash,
      kind: 'transaction',
      role: 'center',
      val: 18
    },
    {
      id: to || 'to',
      label: shortId(to || 'to', 6),
      fullId: to,
      kind: 'address',
      role: 'output',
      val: 12
    }
  ];

  const links = [
    {
      id: `${hash}-in`,
      source: from || 'from',
      target: hash,
      txHash: hash,
      direction: 'in',
      amountBtc: amount,
      amountLabel: `${Number(amount).toFixed(6)} ${unit}`,
      amountUsd: (Number(amount) * price).toFixed(2),
      timestamp,
      index: 1
    },
    {
      id: `${hash}-out`,
      source: hash,
      target: to || 'to',
      txHash: hash,
      direction: 'out',
      amountBtc: amount,
      amountLabel: `${Number(amount).toFixed(6)} ${unit}`,
      amountUsd: (Number(amount) * price).toFixed(2),
      timestamp,
      index: 2
    }
  ];

  return { nodes, links };
}

function buildBlockGraph(blockId, sampleTxs = []) {
  const nodesMap = new Map();
  const links = [];

  nodesMap.set(blockId, {
    id: blockId,
    label: shortId(blockId, 6),
    fullId: blockId,
    kind: 'block',
    role: 'center',
    val: 20
  });

  sampleTxs.slice(0, 12).forEach((tx, i) => {
    const txHash = typeof tx === 'string' ? tx : tx.hash;
    if (!txHash) return;
    nodesMap.set(txHash, {
      id: txHash,
      label: shortId(txHash, 6),
      fullId: txHash,
      kind: 'transaction',
      role: 'counterparty',
      val: 10
    });
    links.push({
      id: `block-tx-${i}`,
      source: blockId,
      target: txHash,
      txHash,
      direction: 'out',
      amountBtc: 0,
      amountLabel: tx.value || `Tx #${i + 1}`,
      amountUsd: null,
      timestamp: tx.timestamp || null,
      index: i + 1
    });
  });

  return { nodes: Array.from(nodesMap.values()), links };
}

module.exports = {
  buildAddressGraph,
  buildTxGraph,
  buildTransferGraph,
  buildSimpleTxGraph,
  buildBlockGraph,
  shortId
};
