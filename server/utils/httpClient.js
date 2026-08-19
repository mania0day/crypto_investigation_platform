const https = require('https');
const axios = require('axios');

const agent = new https.Agent({ family: 4 });

const client = axios.create({
  httpsAgent: agent,
  timeout: 15000
});

module.exports = client;
