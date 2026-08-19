"""Per provider-capability metrics wrapping the whole pipeline.

Tracked (CAPABILITY_MATRIX.md §8): success count, failure count (split into
unavailable / invalid), rate-limited count, latency p50/p95/max over a
sliding window, cache hits, and fallbacks. Snapshots are plain dicts so the
API can expose them directly; a Prometheus exporter can wrap this registry
later without touching call sites.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

_LATENCY_WINDOW = 256


@dataclass
class _Series:
    success: int = 0
    unavailable: int = 0
    invalid: int = 0
    rate_limited: int = 0
    latency_max: float = 0.0
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=_LATENCY_WINDOW))


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


class MetricsRegistry:
    def __init__(self) -> None:
        self._series: dict[tuple[str, str], _Series] = {}
        self._cache_hits: dict[tuple[str, str], int] = {}
        self._fallbacks: dict[tuple[str, str], int] = {}

    def _get(self, provider: str, capability: str) -> _Series:
        return self._series.setdefault((provider, capability), _Series())

    def record_success(self, provider: str, capability: str, seconds: float) -> None:
        series = self._get(provider, capability)
        series.success += 1
        series.latencies.append(seconds)
        series.latency_max = max(series.latency_max, seconds)

    def record_error(self, provider: str, capability: str, kind: str) -> None:
        series = self._get(provider, capability)
        if kind == "rate_limited":
            series.rate_limited += 1
        elif kind == "unavailable":
            series.unavailable += 1
        elif kind == "invalid":
            series.invalid += 1
        else:  # pragma: no cover - defensive
            raise ValueError(f"unknown error kind: {kind}")

    def record_cache_hit(self, chain: str, capability: str) -> None:
        key = (chain, capability)
        self._cache_hits[key] = self._cache_hits.get(key, 0) + 1

    def record_fallback(self, chain: str, capability: str) -> None:
        key = (chain, capability)
        self._fallbacks[key] = self._fallbacks.get(key, 0) + 1

    def snapshot(self) -> dict[str, object]:
        providers: dict[str, dict[str, float | int]] = {}
        for (provider, capability), s in self._series.items():
            attempts = s.success + s.unavailable + s.invalid
            ordered = sorted(s.latencies)
            providers[f"{provider}/{capability}"] = {
                "success": s.success,
                "unavailable": s.unavailable,
                "invalid": s.invalid,
                "rate_limited": s.rate_limited,
                "success_rate": (s.success / attempts) if attempts else 0.0,
                "latency_p50": _percentile(ordered, 0.50),
                "latency_p95": _percentile(ordered, 0.95),
                "latency_max": s.latency_max,
            }
        return {
            "providers": providers,
            "cache_hits": {f"{c}/{cap}": n for (c, cap), n in self._cache_hits.items()},
            "fallbacks": {f"{c}/{cap}": n for (c, cap), n in self._fallbacks.items()},
        }
