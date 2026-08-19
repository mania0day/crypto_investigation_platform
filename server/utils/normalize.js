const normalizeTransaction = (fields) => {
  return { type: 'transaction', ...fields };
};

/**
 * Supports:
 * - object form: normalizeBlock({ chain, blockNumber, hash, ... })
 * - legacy form: normalizeBlock(chain, blockNumber, hash, timestamp, txCount, raw)
 */
const normalizeBlock = (chainOrFields, blockNumber, hash, timestamp, txCount, raw) => {
  if (chainOrFields && typeof chainOrFields === 'object' && !Array.isArray(chainOrFields)) {
    return { type: 'block', status: 'success', ...chainOrFields };
  }

  return {
    type: 'block',
    status: 'success',
    chain: chainOrFields,
    blockNumber,
    hash,
    timestamp,
    txCount,
    raw
  };
};

const normalizeAddress = (fields) => {
  return { type: 'address', ...fields };
};

module.exports = { normalizeTransaction, normalizeBlock, normalizeAddress };
