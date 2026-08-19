"""Analysis — Class F intelligence: consumers, never sources.

Everything here operates exclusively on normalized investigation data and
vendored label datasets. No module in this package may call a blockchain
API (vision principle 1, structurally: nothing here imports the pool).

    analysis/
      attribution/   label records, labelpack loading, the Attributor impl
      sanctions/     OFAC dataset (vendored, MIT-licensed snapshot)
      heuristics/    deterministic detectors over stored movements
      bridges/       (deferred: needs adapters to emit BridgeHints first)
      clustering/    (deferred)
      ml/            (out of v1 scope by decision)
"""
