/**
 * Validate and classify a search query for a given chain.
 * Returns { type: 'address'|'tx'|'block', query } or { error: string }.
 */

function isPositiveInt(q) {
  return /^\d+$/.test(q) && q.length <= 12 && Number(q) >= 0;
}

export function validateAndDetect(query, chain) {
  const q = (query || '').trim();

  if (!q) {
    return { error: 'Enter an address, transaction hash, or block number.' };
  }

  if (q.length > 120) {
    return { error: 'Query is too long. Check that you pasted a valid hash or address.' };
  }

  if (chain === 'bitcoin') {
    if (isPositiveInt(q)) {
      return { type: 'block', query: q };
    }
    // Bech32 / legacy / P2SH addresses
    if (/^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}$/i.test(q)) {
      return { type: 'address', query: q };
    }
    // Block or tx hash (64 hex). Prefer tx; block hash also works via /block/:id
    if (/^[a-fA-F0-9]{64}$/.test(q)) {
      return { type: 'tx', query: q.toLowerCase(), alsoTryBlock: true };
    }
    // Bitcoin block hash sometimes searched explicitly with prefix
    if (/^block:[a-fA-F0-9]{64}$/i.test(q)) {
      return { type: 'block', query: q.slice(6).toLowerCase() };
    }
    return {
      error:
        'Invalid Bitcoin query. Use a wallet address (bc1… / 1… / 3…), a 64-char tx hash, or a block height (e.g. 800000).'
    };
  }

  if (chain === 'ethereum') {
    if (isPositiveInt(q)) {
      return { type: 'block', query: q };
    }
    if (/^0x[a-fA-F0-9]{40}$/.test(q)) {
      return { type: 'address', query: q };
    }
    if (/^0x[a-fA-F0-9]{64}$/.test(q)) {
      return { type: 'tx', query: q };
    }
    // Allow bare 40/64 hex and normalize
    if (/^[a-fA-F0-9]{40}$/.test(q)) {
      return { type: 'address', query: `0x${q}` };
    }
    if (/^[a-fA-F0-9]{64}$/.test(q)) {
      return { type: 'tx', query: `0x${q}` };
    }
    return {
      error:
        'Invalid Ethereum query. Use 0x + 40 hex (address), 0x + 64 hex (tx), or a block number.'
    };
  }

  if (chain === 'tron') {
    if (isPositiveInt(q)) {
      return { type: 'block', query: q };
    }
    // Base58 address
    if (/^T[1-9A-HJ-NP-Za-km-z]{33}$/.test(q)) {
      return { type: 'address', query: q };
    }
    // Hex address (41 + 20 bytes)
    if (/^41[a-fA-F0-9]{40}$/i.test(q)) {
      return { type: 'address', query: q };
    }
    // Tx hash 64 hex (optional 0x)
    if (/^(0x)?[a-fA-F0-9]{64}$/i.test(q)) {
      return { type: 'tx', query: q.replace(/^0x/i, '').toLowerCase() };
    }
    return {
      error:
        'Invalid Tron query. Use a T… address, a 41… hex address, a 64-char tx hash, or a block number (e.g. 83603492).'
    };
  }

  return { error: 'Unsupported chain.' };
}

/** Back-compat helper used by older call sites */
export function detectQueryType(query, chain) {
  const result = validateAndDetect(query, chain);
  if (result.error) return 'tx';
  return result.type;
}
