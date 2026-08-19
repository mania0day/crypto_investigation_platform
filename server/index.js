const dns = require('dns');
dns.setDefaultResultOrder('ipv4first');
const express = require('express');
const cors = require('cors');
const dotenv = require('dotenv');

dotenv.config();

const app = express();
app.use(cors());
app.use(express.json());

// Routes
app.use('/api/bitcoin', require('./routes/bitcoin'));
app.use('/api/ethereum', require('./routes/ethereum'));
app.use('/api/tron', require('./routes/tron'));

const PORT = process.env.PORT || 4000;

app.get('/api/health', (_req, res) => {
  res.json({
    ok: true,
    keys: {
      bitcoin: Boolean(process.env.BITCOIN_API_KEY),
      ethereum: Boolean(process.env.ETHERSCAN_API_KEY),
      tron: Boolean(process.env.TRONGRID_API_KEY)
    }
  });
});

app.listen(PORT, () => {
  console.log(`Server is running on port ${PORT}`);
  console.log('API keys configured:');
  console.log(`  Bitcoin (optional): ${process.env.BITCOIN_API_KEY ? 'yes' : 'no (using free Blockstream)'}`);
  console.log(`  Ethereum:           ${process.env.ETHERSCAN_API_KEY ? 'yes' : 'MISSING'}`);
  console.log(`  Tron:               ${process.env.TRONGRID_API_KEY ? 'yes' : 'MISSING'}`);
});
