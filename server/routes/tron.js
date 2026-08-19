const express = require('express');
const axios = require('../utils/httpClient');
const { normalizeTransaction, normalizeBlock, normalizeAddress } = require('../utils/normalize');
const { getPrices } = require('../utils/priceService');
const { buildTransferGraph, buildSimpleTxGraph, buildBlockGraph } = require('../utils/graphBuilder');

const router = express.Router();
const TRONGRID_API = 'https://api.trongrid.io';

const getHeaders = () => {
  const apiKey = process.env.TRONGRID_API_KEY;
  if (!apiKey) throw new Error('TRONGRID_API_KEY is not configured.');
  return { 'TRON-PRO-API-KEY': apiKey };
};

router.get('/address/:address', async (req, res) => {
  try {
    const { address } = req.params;

    const [accountRes, txsRes, prices] = await Promise.all([
      axios.get(`${TRONGRID_API}/v1/accounts/${address}`, { headers: getHeaders() }),
      axios.get(`${TRONGRID_API}/v1/accounts/${address}/transactions`, {
        headers: getHeaders(),
        params: { limit: 10, only_confirmed: true }
      }),
      getPrices()
    ]);

    const account = accountRes.data?.data?.[0];
    if (!account) {
      return res.status(404).json({ error: 'Address not found.' });
    }

    const trxPrice = prices.tron?.usd || 0;
    const balanceTrx = (account.balance || 0) / 1000000;
    const txList = txsRes.data?.data || [];

    const recentTxs = txList.slice(0, 10).map((tx) => {
      const contract = tx.raw_data?.contract?.[0];
      const value = contract?.parameter?.value;
      const amountSun = value?.amount || 0;
      return {
        hash: tx.txID,
        from: value?.owner_address || 'Unknown',
        to: value?.to_address || 'Unknown',
        value: (amountSun / 1e6).toFixed(6) + ' TRX',
        amount: amountSun / 1e6,
        status: tx.ret?.[0]?.contractRet === 'SUCCESS' ? 'success' : 'failed',
        timestamp: tx.block_timestamp
          ? new Date(tx.block_timestamp).toISOString()
          : 'Unknown'
      };
    });

    const graph = buildTransferGraph(
      address,
      recentTxs.map((t) => ({
        hash: t.hash,
        from: t.from,
        to: t.to,
        amount: t.amount,
        timestamp: t.timestamp === 'Unknown' ? null : t.timestamp
      })),
      { unit: 'TRX', price: trxPrice, kind: 'address' }
    );

    const normalized = normalizeAddress({
      chain: 'tron',
      address,
      balance: balanceTrx.toFixed(6) + ' TRX',
      balanceUsd: (balanceTrx * trxPrice).toFixed(2),
      txCount: (account.transactions_in || 0) + (account.transactions_out || 0) || txList.length,
      createTime: account.create_time ? new Date(account.create_time).toISOString() : null,
      recentTxs,
      graph,
      status: 'success',
      timestamp: new Date().toISOString(),
      raw: { account, transactions: txsRes.data }
    });

    res.json(normalized);
  } catch (error) {
    console.error('Tron address error:', error.code, error.message);
    res.status(500).json({ error: error.message || 'Internal server error.' });
  }
});

router.get('/tx/:hash', async (req, res) => {
  try {
    const { hash } = req.params;

    const [txRes, infoRes, nowBlockRes, prices] = await Promise.all([
      axios.post(`${TRONGRID_API}/wallet/gettransactionbyid`, { value: hash }, { headers: getHeaders() }),
      axios.post(`${TRONGRID_API}/wallet/gettransactioninfobyid`, { value: hash }, { headers: getHeaders() }),
      axios.post(`${TRONGRID_API}/wallet/getnowblock`, {}, { headers: getHeaders() }),
      getPrices()
    ]);

    const data = txRes.data;
    if (!data || Object.keys(data).length === 0 || data.Error) {
      return res.status(404).json({ error: 'Transaction not found.' });
    }

    const info = infoRes.data;
    const trxPrice = prices.tron?.usd || 0;

    let from = 'Unknown';
    let to = 'Unknown';
    let valueTrx = 0;
    let contractType = 'Unknown';

    if (data.raw_data && data.raw_data.contract && data.raw_data.contract[0]) {
      const contract = data.raw_data.contract[0];
      contractType = contract.type;
      if (contract.type === 'TransferContract') {
        const valueSun = contract.parameter.value.amount || 0;
        valueTrx = valueSun / 1000000;
        from = contract.parameter.value.owner_address || 'Unknown';
        to = contract.parameter.value.to_address || 'Unknown';
      }
    }

    const blockNumber = info.blockNumber || null;
    const currentBlock = nowBlockRes.data?.block_header?.raw_data?.number || 0;
    const confirmations = blockNumber ? Math.max(currentBlock - blockNumber + 1, 0) : 0;

    let blockId = info.blockHash || info.blockID || null;
    if (!blockId && blockNumber) {
      try {
        const blockRes = await axios.post(
          `${TRONGRID_API}/wallet/getblockbynum`,
          { num: blockNumber },
          { headers: getHeaders() }
        );
        blockId = blockRes.data?.blockID || null;
      } catch (e) {
        console.warn('Could not resolve Tron block id:', e.message);
      }
    }

    const feeSun = info.fee || 0;
    const feeTrx = feeSun / 1000000;

    const netFeeSun = info.receipt?.net_fee || 0;
    const energyFeeSun = info.receipt?.energy_fee || 0;
    const energyUsage = info.receipt?.energy_usage_total || 0;
    const netUsage = info.receipt?.net_usage || 0;

    const normalized = normalizeTransaction({
      chain: 'tron',
      hash: data.txID,
      blockNumber: blockNumber || 'Unknown',
      blockId: blockId || null,
      from,
      to,
      value: valueTrx.toFixed(6) + ' TRX',
      valueUsd: (valueTrx * trxPrice).toFixed(2),
      fee: feeTrx.toFixed(6) + ' TRX',
      feeUsd: (feeTrx * trxPrice).toFixed(2),
      confirmations,
      timestamp: data.raw_data && data.raw_data.timestamp ? new Date(data.raw_data.timestamp).toISOString() : 'Unknown',
      status: data.ret && data.ret[0] && data.ret[0].contractRet === 'SUCCESS' ? 'success' : 'failed',
      contractType,
      energyUsage,
      netUsage,
      netFeeTrx: (netFeeSun / 1000000).toFixed(6),
      energyFeeTrx: (energyFeeSun / 1000000).toFixed(6),
      expiration: data.raw_data?.expiration ? new Date(data.raw_data.expiration).toISOString() : null,
      graph: buildSimpleTxGraph(
        data.txID,
        from,
        to,
        valueTrx,
        'TRX',
        trxPrice,
        data.raw_data?.timestamp ? new Date(data.raw_data.timestamp).toISOString() : null
      ),
      raw: { transaction: data, info }
    });

    res.json(normalized);
  } catch (error) {
    console.error('Tron tx error:', error.code, error.message);
    res.status(500).json({ error: error.message || 'Internal server error.' });
  }
});

router.get('/block/:id', async (req, res) => {
  try {
    const { id } = req.params;
    const num = parseInt(id, 10);
    if (Number.isNaN(num) || num < 0) {
      return res.status(400).json({ error: 'Invalid Tron block number.' });
    }

    const response = await axios.post(
      `${TRONGRID_API}/wallet/getblockbynum`,
      { num },
      { headers: getHeaders() }
    );

    const data = response.data;
    if (!data || Object.keys(data).length === 0 || data.Error) {
      return res.status(404).json({ error: 'Block not found.' });
    }

    const txs = Array.isArray(data.transactions) ? data.transactions : [];
    const sampleTxs = txs.slice(0, 12).map((tx, i) => {
      const contract = tx.raw_data?.contract?.[0];
      const value = contract?.parameter?.value;
      const amount = value?.amount != null ? (value.amount / 1e6).toFixed(6) + ' TRX' : `Tx #${i + 1}`;
      return {
        hash: tx.txID,
        from: value?.owner_address || null,
        to: value?.to_address || null,
        value: amount,
        timestamp: tx.raw_data?.timestamp
          ? new Date(tx.raw_data.timestamp).toISOString()
          : null
      };
    });

    const header = data.block_header?.raw_data || {};

    const normalized = normalizeBlock({
      chain: 'tron',
      blockNumber: header.number,
      hash: data.blockID,
      timestamp: header.timestamp ? new Date(header.timestamp).toISOString() : null,
      txCount: txs.length,
      parentHash: header.parentHash || null,
      witnessAddress: header.witness_address || null,
      version: header.version ?? null,
      sampleTxs,
      graph: buildBlockGraph(data.blockID, sampleTxs),
      raw: data
    });

    res.json(normalized);
  } catch (error) {
    console.error('Tron block error:', error.code, error.message);
    res.status(500).json({ error: error.message || 'Internal server error.' });
  }
});

module.exports = router;
