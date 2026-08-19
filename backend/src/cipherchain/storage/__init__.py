"""Postgres persistence — the approved schema (docs/research/STORAGE_SCHEMA.md).

Two planes:
- Global immutable fact store (addresses, transactions, movements, assets,
  provider_cache): written once, shared across investigations, provenance on
  every fact row. A repeat fetch of stored data is a bug.
- Per-investigation overlay (investigations, nodes, edges, findings,
  evidence): cascade-deleted with the investigation. ``nodes`` doubles as
  the checkpointed frontier that makes investigations resumable.

All access goes through repositories; nothing outside this package writes SQL.
"""
