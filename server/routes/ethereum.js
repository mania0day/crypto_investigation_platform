const express = require('express');
const axios = require('../utils/httpClient');
const { normalizeTransaction, normalizeBlock, normalizeAddress } = require('../utils/normalize');
const { getPrices } = require('../utils/priceService');
const { buildTransferGraph, buildSimpleTxGraph, buildBlockGraph } = require('../utils/graphBuilder');

const router = express.Router();
const ETHERSCAN_API = 'https://api.etherscan.io/v2/api';

router.get('/address/:address', async (req, res) => {
  try {
    const { address } = req.params;
    const apiKey = process.env.ETHERSCAN_API_KEY;

    if (!apiKey) return res.status(500).json({ error: 'ETHERSCAN_API_KEY is not configured.' });

    const [balanceRes, txListRes, prices] = await Promise.all([
      axios.get(ETHERSCAN_API, {
        params: { chainid: 1, module: 'account', action: 'balance', address, tag: 'latest', apikey: apiKey }
      }),
      axios.get(ETHERSCAN_API, {
        params: {
          chainid: 1,
          module: 'account',
          action: 'txlist',
          address,
          startblock: 0,
          endblock: 99999999,
          page: 1,
          offset: 10,
          sort: 'desc',
          apikey: apiKey
        }
      }),
      getPrices()
    ]);

    if (balanceRes.data.status === '0' && balanceRes.data.message === 'NOTOK') {
      return res.status(404).json({ error: balanceRes.data.result || 'Address lookup failed.' });
    }

    const ethPrice = prices.ethereum?.usd || 0;
    const balanceWei = BigInt(balanceRes.data.result || 0);
    const balanceEth = Number(balanceWei) / 1e18;

    const txList = Array.isArray(txListRes.data.result) ? txListRes.data.result : [];
    const recentTxs = txList.slice(0, 10).map((tx) => ({
      hash: tx.hash,
      from: tx.from,
      to: tx.to,
      value: (Number(tx.value) / 1e18).toFixed(6) + ' ETH',
      amount: Number(tx.value) / 1e18,
      status: tx.txreceipt_status === '1' || tx.isError === '0' ? 'success' : 'failed',
      blockNumber: Number(tx.blockNumber),
      timestamp: new Date(Number(tx.timeStamp) * 1000).toISOString()
    }));

    const graph = buildTransferGraph(
      address,
      recentTxs.map((t) => ({
        hash: t.hash,
        from: t.from,
        to: t.to,
        amount: t.amount,
        timestamp: t.timestamp
      })),
      { unit: 'ETH', price: ethPrice, kind: 'address' }
    );

    const normalized = normalizeAddress({
      chain: 'ethereum',
      address,
      balance: balanceEth.toFixed(6) + ' ETH',
      balanceUsd: (balanceEth * ethPrice).toFixed(2),
      txCount: txList.length,
      recentTxs,
      graph,
      status: 'success',
      timestamp: new Date().toISOString(),
      raw: { balance: balanceRes.data, transactions: txListRes.data }
    });

    res.json(normalized);
  } catch (error) {
    console.error('Ethereum address error:', error.code, error.message);
    res.status(500).json({ error: 'Internal server error.' });
  }
});

router.get('/tx/:hash', async (req, res) => {
  try {
    const { hash } = req.params;
    const apiKey = process.env.ETHERSCAN_API_KEY;

    if (!apiKey) return res.status(500).json({ error: 'ETHERSCAN_API_KEY is not configured.' });

    const [txRes, blockNumRes, prices] = await Promise.all([
      axios.get(ETHERSCAN_API, {
        params: { chainid: 1, module: 'proxy', action: 'eth_getTransactionByHash', txhash: hash, apikey: apiKey }
      }),
      axios.get(ETHERSCAN_API, {
        params: { chainid: 1, module: 'proxy', action: 'eth_blockNumber', apikey: apiKey }
      }),
      getPrices()
    ]);

    const data = txRes.data;
    if (!data.result || typeof data.result !== 'object') {
      return res.status(404).json({ error: 'Transaction not found.' });
    }

    const tx = data.result;
    const ethPrice = prices.ethereum?.usd || 0;

    const valueWei = BigInt(tx.value || 0);
    const valueEth = Number(valueWei) / 1e18;

    const blockNumber = parseInt(tx.blockNumber, 16);
    const currentBlock = parseInt(blockNumRes.data.result, 16);
    const confirmations = blockNumber ? Math.max(currentBlock - blockNumber + 1, 0) : 0;

    let gasUsed = null;
    let feeEth = 0;
    try {
      const receiptRes = await axios.get(ETHERSCAN_API, {
        params: { chainid: 1, module: 'proxy', action: 'eth_getTransactionReceipt', txhash: hash, apikey: apiKey }
      });
      gasUsed = BigInt(receiptRes.data.result?.gasUsed || 0);
      const gasPrice = BigInt(tx.gasPrice || 0);
      feeEth = Number(gasUsed * gasPrice) / 1e18;
    } catch (e) {
      const gasLimit = BigInt(tx.gas || 0);
      const gasPrice = BigInt(tx.gasPrice || 0);
      feeEth = Number(gasLimit * gasPrice) / 1e18;
    }

    const gasPriceGwei = Number(BigInt(tx.gasPrice || 0)) / 1e9;
    const isEip1559 = tx.type === '0x2';

    const normalized = normalizeTransaction({
      chain: 'ethereum',
      hash: tx.hash,
      blockNumber,
      blockId: tx.blockHash || null,
      from: tx.from,
      to: tx.to,
      value: valueEth.toFixed(6) + ' ETH',
      valueUsd: (valueEth * ethPrice).toFixed(2),
      fee: feeEth.toFixed(8) + ' ETH',
      feeUsd: (feeEth * ethPrice).toFixed(2),
      confirmations,
      timestamp: tx.blockTimestamp ? new Date(parseInt(tx.blockTimestamp, 16) * 1000).toISOString() : 'Unknown',
      status: blockNumber ? 'success' : 'pending',
      nonce: parseInt(tx.nonce, 16),
      transactionIndex: parseInt(tx.transactionIndex, 16),
      gasLimit: parseInt(tx.gas, 16),
      gasUsed: gasUsed !== null ? Number(gasUsed) : null,
      gasPriceGwei: gasPriceGwei.toFixed(4),
      transactionType: isEip1559 ? 'EIP-1559' : 'Legacy',
      inputData: tx.input && tx.input !== '0x' ? tx.input : null,
      isContractInteraction: !!(tx.input && tx.input !== '0x'),
      graph: buildSimpleTxGraph(
        tx.hash,
        tx.from,
        tx.to,
        valueEth,
        'ETH',
        ethPrice,
        tx.blockTimestamp ? new Date(parseInt(tx.blockTimestamp, 16) * 1000).toISOString() : null
      ),
      raw: tx
    });

    res.json(normalized);
  } catch (error) {
    console.error('Ethereum tx error:', error.code, error.message);
    res.status(500).json({ error: 'Internal server error.' });
  }
});

router.get('/block/:id', async (req, res) => {
  try {
    const { id } = req.params;
    const apiKey = process.env.ETHERSCAN_API_KEY;

    if (!apiKey) return res.status(500).json({ error: 'ETHERSCAN_API_KEY is not configured.' });

    let tag = id;
    if (!id.startsWith('0x')) {
      tag = '0x' + parseInt(id, 10).toString(16);
    }

    const response = await axios.get(ETHERSCAN_API, {
      params: {
        chainid: 1,
        module: 'proxy',
        action: 'eth_getBlockByNumber',
        tag,
        boolean: 'true',
        apikey: apiKey
      }
    });

    const data = response.data;
    if (!data.result) {
      return res.status(404).json({ error: 'Block not found.' });
    }

    const block = data.result;
    const txs = Array.isArray(block.transactions) ? block.transactions : [];
    const sampleTxs = txs.slice(0, 12).map((tx, i) => {
      if (typeof tx === 'string') {
        return { hash: tx, value: `Tx #${i + 1}` };
      }
      const valueEth = Number(BigInt(tx.value || 0)) / 1e18;
      return {
        hash: tx.hash,
        from: tx.from,
        to: tx.to,
        value: `${valueEth.toFixed(6)} ETH`,
        timestamp: block.timestamp
          ? new Date(parseInt(block.timestamp, 16) * 1000).toISOString()
          : null
      };
    });

    const normalized = normalizeBlock({
      chain: 'ethereum',
      blockNumber: parseInt(block.number, 16),
      hash: block.hash,
      timestamp: new Date(parseInt(block.timestamp, 16) * 1000).toISOString(),
      txCount: txs.length,
      miner: block.miner,
      parentHash: block.parentHash,
      gasUsed: parseInt(block.gasUsed, 16),
      gasLimit: parseInt(block.gasLimit, 16),
      difficulty: block.difficulty ? parseInt(block.difficulty, 16) : null,
      size: block.size ? parseInt(block.size, 16) : null,
      nonce: block.nonce,
      sampleTxs,
      graph: buildBlockGraph(block.hash, sampleTxs),
      raw: block
    });

    res.json(normalized);
  } catch (error) {
    console.error('Ethereum block error:', error.code, error.message);
    res.status(500).json({ error: 'Internal server error.' });
  }
});

module.exports = router;
