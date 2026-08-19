"""Shared transport plumbing for vendor clients.

Maps the transport-level outcomes every vendor shares; clients keep their
own 4xx and body semantics.
"""

from __future__ import annotations

from typing import Any

import httpx

from cipherchain.core.errors import ProviderRateLimited, ProviderUnavailable


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


async def perform(
    http: httpx.AsyncClient, provider_name: str, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    """Issue a request; translate transport failures, 429, and 5xx."""
    try:
        response = await http.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise ProviderUnavailable(provider_name, f"transport error: {exc!r}") from exc
    if response.status_code == 429:
        raise ProviderRateLimited(
            provider_name, "HTTP 429", retry_after=_retry_after_seconds(response)
        )
    if response.status_code >= 500:
        raise ProviderUnavailable(provider_name, f"HTTP {response.status_code}")
    return response
