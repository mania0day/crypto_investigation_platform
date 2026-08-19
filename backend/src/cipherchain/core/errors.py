"""CipherChain error hierarchy.

Provider errors carry the provider name so the pool's metrics and circuit
breaker can attribute failures without parsing messages.
"""

from __future__ import annotations


class CipherChainError(Exception):
    """Base class for all CipherChain errors."""


class ConfigurationError(CipherChainError):
    """Missing or contradictory configuration."""


class ProviderError(CipherChainError):
    """A single provider call failed."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider


class ProviderRateLimited(ProviderError):
    """Provider returned a rate-limit response (e.g. HTTP 429)."""

    def __init__(self, provider: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(provider, message)
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    """Transport failure or 5xx — the provider could not answer at all."""


class ProviderResponseInvalid(ProviderError):
    """The provider answered, but the payload violates its own contract.

    Never retried against the same provider: a malformed answer is not
    transient.
    """


class ResourceNotFound(ProviderError):
    """The provider answered authoritatively that the resource does not exist.

    This is an answer, not a failure: the pool must propagate it without
    retry or failover, and callers must treat it as evidence-relevant
    information (e.g. an unknown tx hash).
    """


class AllProvidersFailed(CipherChainError):
    """Every provider able to serve (chain, capability) failed or is open-circuited."""

    def __init__(self, chain: str, capability: str, attempts: int) -> None:
        super().__init__(f"no provider could serve {capability} on {chain} ({attempts} attempt(s))")
        self.chain = chain
        self.capability = capability
        self.attempts = attempts


class CapabilityNotSupported(CipherChainError):
    """The chain's adapter declares this capability absent (vision principle 4).

    A declared absence is not a failure; callers must degrade explicitly.
    """

    def __init__(self, chain: str, capability: str) -> None:
        super().__init__(f"{chain} does not support {capability}")
        self.chain = chain
        self.capability = capability


class UnknownChain(CipherChainError):
    """No adapter is registered for the requested chain."""

    def __init__(self, chain: str) -> None:
        super().__init__(f"no adapter registered for chain {chain!r}")
        self.chain = chain


class BudgetExhausted(CipherChainError):
    """An investigation budget ran out; the investigation must end as partial."""

    def __init__(self, budget: str, limit: int | float) -> None:
        super().__init__(f"budget exhausted: {budget} (limit {limit})")
        self.budget = budget
        self.limit = limit
