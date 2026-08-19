from cipherchain.providers.metrics import MetricsRegistry


def test_snapshot_shapes_and_percentiles() -> None:
    registry = MetricsRegistry()
    for latency in (0.1, 0.2, 0.3, 0.4, 1.0):
        registry.record_success("etherscan", "address_history", latency)
    registry.record_error("etherscan", "address_history", "unavailable")
    registry.record_error("etherscan", "address_history", "rate_limited")
    registry.record_cache_hit("ethereum", "tx_lookup")
    registry.record_fallback("ethereum", "address_history")

    snapshot = registry.snapshot()
    series = snapshot["providers"]["etherscan/address_history"]
    assert series["success"] == 5
    assert series["unavailable"] == 1
    assert series["rate_limited"] == 1
    assert series["success_rate"] == 5 / 6  # rate-limits excluded from failure rate
    assert series["latency_p50"] == 0.3
    assert series["latency_p95"] == 1.0
    assert series["latency_max"] == 1.0
    assert snapshot["cache_hits"] == {"ethereum/tx_lookup": 1}
    assert snapshot["fallbacks"] == {"ethereum/address_history": 1}
