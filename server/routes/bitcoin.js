const express = require('express');
const axios = require('../utils/httpClient');
const { normalizeTransaction, normalizeBlock, normalizeAddress } = require('../utils/normalize');
const { getPrices } = require('../utils/priceService');
const { buildAddressGraph, buildTxGraph, buildBlockGraph } = require('../utils/graphBuilder');

const router = express.Router();
const BLOCKSTREAM_API = 'https://blockstream.info/api';
const BLOCKCYPHER_API = 'https://api.blockcypher.com/v1/btc/main';

const getBitcoinApiKey = () => process.env.BITCOIN_API_KEY || '';

async function fetchTxFromBlockstream(hash) {
  const [txResponse, tipHeightResponse] = await Promise.all([
    axios.get(`${BLOCKSTREAM_API}/tx/${hash}`),
    axios.get(`${BLOCKSTREAM_API}/blocks/tip/height`)
  ]);
  return { source: 'blockstream', tx: txResponse.data, tipHeight: tipHeightResponse.data };
}

async function fetchTxFromBlockcypher(hash, apiKey) {
  const response = await axios.get(`${BLOCKCYPHER_API}/txs/${hash}`, {
    params: apiKey ? { token: apiKey } : undefined
  });
  const tipRes = await axios.get(`${BLOCKCYPHER_API}`, {
    params: apiKey ? { token: apiKey } : undefined
  });
  return { source: 'blockcypher', tx: response.data, tipHeight: tipRes.data.height };
}

function normalizeBlockstreamTx(data, tipHeight, btcPrice) {
  const totalOutSats = data.vout.reduce((sum, out) => sum + out.value, 0);
  const totalInSats = data.vin.reduce((sum, inp) => sum + (inp.prevout?.value || 0), 0);
  const feeSats = data.vin[0]?.is_coinbase ? 0 : Math.max(totalInSats - totalOutSats, 0);

  const valueBTC = totalOutSats / 100000000;
  const feeBTC = feeSats / 100000000;

  const fromAddress = data.vin[0]?.prevout?.scriptpubkey_address
    || (data.vin[0]?.is_coinbase ? 'Coinbase (newly mined)' : 'Unknown');
  const toAddress = data.vout[0]?.scriptpubkey_address || 'Unknown';

  const confirmations = data.status.confirmed ? (tipHeight - data.status.block_height + 1) : 0;
  const rbfEnabled = data.vin.some((inp) => inp.sequence < 0xfffffffe);
  const hasWitness = data.vin.some((inp) => inp.witness && inp.witness.length > 0);
  const opReturnOutputs = data.vout.filter((out) => out.scriptpubkey_type === 'op_return');
  const opReturnData = opReturnOutputs.map((out) => out.scriptpubkey_asm).join(' | ') || null;

  return normalizeTransaction({
    chain: 'bitcoin',
    hash: data.txid,
    blockNumber: data.status.block_height,
    blockId: data.status.block_hash || null,
    from: fromAddress,
    to: toAddress,
    value: valueBTC.toFixed(8) + ' BTC',
    valueUsd: (valueBTC * btcPrice).toFixed(2),
    fee: feeBTC.toFixed(8) + ' BTC',
    feeUsd: (feeBTC * btcPrice).toFixed(2),
    confirmations,
    timestamp: data.status.block_time
      ? new Date(data.status.block_time * 1000).toISOString()
      : 'Unconfirmed',
    status: data.status.confirmed ? 'success' : 'pending',
    size: data.size,
    weight: data.weight,
    virtualSize: Math.ceil(data.weight / 4),
    version: data.version,
    lockTime: data.locktime,
    rbfEnabled,
    isCoinbaseTx: data.vin[0]?.is_coinbase || false,
    hasWitness,
    opReturnData,
    raw: data
  });
}

function normalizeBlockcypherTx(data, tipHeight, btcPrice) {
  const valueBTC = (data.total || 0) / 100000000;
  const feeBTC = (data.fees || 0) / 100000000;
  const fromAddress = data.inputs?.[0]?.addresses?.[0]
    || (data.inputs?.[0]?.output_index === -1 ? 'Coinbase (newly mined)' : 'Unknown');
  const toAddress = data.outputs?.[0]?.addresses?.[0] || 'Unknown';
  const confirmations = data.confirmations || 0;

  return normalizeTransaction({
    chain: 'bitcoin',
    hash: data.hash,
    blockNumber: data.block_height || null,
    blockId: data.block_hash || null,
    from: fromAddress,
    to: toAddress,
    value: valueBTC.toFixed(8) + ' BTC',
    valueUsd: (valueBTC * btcPrice).toFixed(2),
    fee: feeBTC.toFixed(8) + ' BTC',
    feeUsd: (feeBTC * btcPrice).toFixed(2),
    confirmations,
    timestamp: data.confirmed || data.received || 'Unconfirmed',
    status: confirmations > 0 ? 'success' : 'pending',
    size: data.size,
    weight: null,
    virtualSize: null,
    version: data.ver,
    lockTime: data.lock_time,
    rbfEnabled: !!data.preference && data.preference === 'low',
    isCoinbaseTx: data.inputs?.[0]?.output_index === -1,
    hasWitness: false,
    opReturnData: null,
    tipHeight,
    raw: data
  });
}

router.get('/tx/:hash', async (req, res) => {
  try {
    const { hash } = req.params;
    const apiKey = getBitcoinApiKey();
    const prices = await getPrices();
    const btcPrice = prices.bitcoin?.usd || 0;

    let result;
    if (apiKey) {
      try {
        result = await fetchTxFromBlockcypher(hash, apiKey);
      } catch (err) {
        console.warn('BlockCypher failed, falling back to Blockstream:', err.message);
        result = await fetchTxFromBlockstream(hash);
      }
    } else {
      result = await fetchTxFromBlockstream(hash);
    }

    const normalized = result.source === 'blockcypher'
      ? normalizeBlockcypherTx(result.tx, result.tipHeight, btcPrice)
      : normalizeBlockstreamTx(result.tx, result.tipHeight, btcPrice);

    const graph = buildTxGraph(result.tx, btcPrice);
    normalized.graph = { nodes: graph.nodes, links: graph.links };
    normalized.inputs = graph.inputs;
    normalized.outputs = graph.outputs;

    res.json(normalized);
  } catch (error) {
    console.error('Bitcoin tx error:', error.code, error.message);
    res.status(404).json({ error: 'Transaction not found or API error.' });
  }
});

router.get('/address/:address', async (req, res) => {
  try {
    const { address } = req.params;
    const prices = await getPrices();
    const btcPrice = prices.bitcoin?.usd || 0;

    const [addrRes, txsRes] = await Promise.all([
      axios.get(`${BLOCKSTREAM_API}/address/${address}`),
      axios.get(`${BLOCKSTREAM_API}/address/${address}/txs`)
    ]);

    const data = addrRes.data;
    const chain = data.chain_stats || {};
    const mempool = data.mempool_stats || {};

    const funded = (chain.funded_txo_sum || 0) + (mempool.funded_txo_sum || 0);
    const spent = (chain.spent_txo_sum || 0) + (mempool.spent_txo_sum || 0);
    const balanceSats = funded - spent;
    const balanceBtc = balanceSats / 100000000;
    const txCount = (chain.tx_count || 0) + (mempool.tx_count || 0);

    const rawTxs = txsRes.data || [];
    const recentTxs = rawTxs.slice(0, 10).map((tx) => {
      const inputs = (tx.vin || [])
        .map((v) => v.prevout?.scriptpubkey_address)
        .filter(Boolean);
      const outputs = (tx.vout || [])
        .map((v) => v.scriptpubkey_address)
        .filter(Boolean);
      const received = (tx.vout || [])
        .filter((v) => v.scriptpubkey_address === address)
        .reduce((s, v) => s + (v.value || 0), 0);
      const sent = (tx.vin || [])
        .filter((v) => v.prevout?.scriptpubkey_address === address)
        .reduce((s, v) => s + (v.prevout?.value || 0), 0);

      return {
        hash: tx.txid,
        status: tx.status?.confirmed ? 'success' : 'pending',
        blockNumber: tx.status?.block_height || null,
        timestamp: tx.status?.block_time
          ? new Date(tx.status.block_time * 1000).toISOString()
          : 'Unconfirmed',
        from: inputs[0] || 'Unknown',
        to: outputs.find((a) => a !== address) || outputs[0] || 'Unknown',
        value: ((Math.max(received, sent)) / 1e8).toFixed(8) + ' BTC',
        direction: received >= sent ? 'in' : 'out'
      };
    });

    const graph = buildAddressGraph(address, rawTxs, btcPrice);

    const normalized = normalizeAddress({
      chain: 'bitcoin',
      address,
      balance: balanceBtc.toFixed(8) + ' BTC',
      balanceUsd: (balanceBtc * btcPrice).toFixed(2),
      txCount,
      totalReceived: ((chain.funded_txo_sum || 0) / 100000000).toFixed(8) + ' BTC',
      totalSent: ((chain.spent_txo_sum || 0) / 100000000).toFixed(8) + ' BTC',
      recentTxs,
      graph,
      status: 'success',
      timestamp: new Date().toISOString(),
      raw: { address: data, recentTxs: rawTxs }
    });

    res.json(normalized);
  } catch (error) {
    console.error('Bitcoin address error:', error.code, error.message);
    res.status(404).json({ error: 'Address not found or API error.' });
  }
});

router.get('/block/:id', async (req, res) => {
  try {
    const { id } = req.params;
    let blockHash = id;

    if (!isNaN(id) && String(id).length < 15) {
      const hashRes = await axios.get(`${BLOCKSTREAM_API}/block-height/${id}`);
      blockHash = hashRes.data;
    }

    const [response, txidsRes] = await Promise.all([
      axios.get(`${BLOCKSTREAM_API}/block/${blockHash}`),
      axios.get(`${BLOCKSTREAM_API}/block/${blockHash}/txids`).catch(() => ({ data: [] }))
    ]);

    const data = response.data;
    const txids = Array.isArray(txidsRes.data) ? txidsRes.data : [];
    const sampleTxs = txids.slice(0, 12).map((hash, i) => ({
      hash,
      value: `Tx #${i + 1}`
    }));

    const graph = buildBlockGraph(data.id, sampleTxs);

    const normalized = normalizeBlock({
      chain: 'bitcoin',
      blockNumber: data.height,
      hash: data.id,
      timestamp: new Date(data.timestamp * 1000).toISOString(),
      txCount: data.tx_count,
      size: data.size,
      weight: data.weight,
      merkleRoot: data.merkle_root,
      previousBlockHash: data.previousblockhash,
      medianTime: data.mediantime ? new Date(data.mediantime * 1000).toISOString() : null,
      nonce: data.nonce,
      bits: data.bits,
      difficulty: data.difficulty,
      version: data.version,
      sampleTxs,
      graph,
      raw: data
    });

    res.json(normalized);
  } catch (error) {
    console.error('Bitcoin block error:', error.code, error.message);
    res.status(404).json({ error: 'Block not found or API error.' });
  }
});

module.exports = router;
