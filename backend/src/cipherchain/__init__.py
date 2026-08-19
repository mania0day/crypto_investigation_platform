"""CipherChain — autonomous blockchain investigation engine.

Module map (one bounded context each; vision doc 01 governs):

- ``core``          — canonical chain-agnostic domain model, errors, settings
- ``providers``     — Provider SDK: capability-routed pool (cache, limits, breaker, metrics)
- ``chains``        — Chain SDK: adapters that normalize chain data into the canonical model
- ``storage``       — Postgres persistence: global fact store + per-investigation overlays
- ``investigation`` — goal-directed engine: objectives, frontier, budgets, state machine
- ``analysis``      — intelligence consumers: detection/attribution on normalized data only
- ``graph``         — GraphStore port: traversal queries over stored facts
- ``evidence``      — evidence assembly and content addressing
- ``api``           — FastAPI surface

Non-negotiable boundary (vision principle 1): only ``chains`` adapters — via the
``providers`` pool — talk to the outside world. ``analysis``, ``investigation``,
and ``graph`` operate exclusively on normalized, stored data.
"""

__version__ = "0.1.0"
