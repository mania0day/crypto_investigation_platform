const axios = require('./httpClient');

let cache = { data: null, timestamp: 0 };
const CACHE_DURATION = 60 * 1000; // 1 minute

async function getPrices() {
  const now = Date.now();
  if (cache.data && (now - cache.timestamp) < CACHE_DURATION) {
    return cache.data;
  }
  try {
    const response = await axios.get('https://api.coingecko.com/api/v3/simple/price', {
      params: { ids: 'bitcoin,ethereum,tron', vs_currencies: 'usd' }
    });
    cache = { data: response.data, timestamp: now };
    return response.data;
  } catch (error) {
    console.error('Price fetch error:', error.message);
    return cache.data || { bitcoin: { usd: 0 }, ethereum: { usd: 0 }, tron: { usd: 0 } };
  }
}

module.exports = { getPrices };
