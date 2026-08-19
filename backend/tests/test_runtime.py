"""Composition root wires exactly what the configuration provides."""

from collections.abc import AsyncIterator

import httpx
import pytest

from cipherchain.core.config import Settings
from cipherchain.core.models import Capability
from cipherchain.runtime import build_chain_registry, build_provider_pool


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(500))
    ) as client:
        yield client


async def test_registry_serves_configured_chains(http: httpx.AsyncClient) -> None:
    settings = Settings(_env_file=None, etherscan_api_key="k", drpc_api_key="k")
    pool = build_provider_pool(settings, http)
    registry = build_chain_registry(pool)
    assert registry.chains() == ("bitcoin", "ethereum", "polygon", "solana", "tron")
    assert registry.get("ethereum").supports(Capability.TOKEN_TRANSFERS)
    assert registry.get("bitcoin").supports(Capability.UTXO_LOOKUP)
    # Polygon arrived as pure configuration — no adapter code was written.
    assert registry.get("polygon").supports(Capability.TOKEN_TRANSFERS)
    assert registry.get("solana").supports(Capability.ADDRESS_HISTORY)
    # Tron needs no key at all; TronGrid serves it keylessly.
    assert registry.get("tron").supports(Capability.TOKEN_TRANSFERS)


async def test_address_detection_across_all_chains(http: httpx.AsyncClient) -> None:
    """Each format resolves to exactly one chain, except the EVM family,
    which genuinely shares one format and must be reported as ambiguous
    rather than guessed."""
    settings = Settings(_env_file=None, etherscan_api_key="k")
    registry = build_chain_registry(build_provider_pool(settings, http))

    assert registry.detect("0x" + "a" * 40) == ("ethereum", "polygon")
    # Bitcoin legacy must NOT collide with Solana's base58 range
    assert registry.detect("3PeVz6zCzRWsRq9YfZYbfbP92ZYDNyMUCC") == ("bitcoin",)
    assert registry.detect("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa") == ("bitcoin",)
    assert registry.detect("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq") == ("bitcoin",)
    assert registry.detect("DNVZMSqeRH18Xa4MCTrb1MndNf3Npg4MEwqswo23eWkf") == ("solana",)
    # Tron's T-prefix keeps it clear of Bitcoin's [13] and Solana's length
    assert registry.detect("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t") == ("tron",)


async def test_a_keyless_deployment_still_reaches_evm_history(http: httpx.AsyncClient) -> None:
    """With no keys at all, Ethereum history is served by the fallback tier.

    This used to raise with zero candidates. A run losing its Etherscan
    allowance should slow down rather than stop (REACHING_THE_VASP.md §5), so
    the keyless Blockscout provider is always registered — last.
    """
    from cipherchain.providers.base import ProviderRequest

    served: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        served.append(str(request.url))
        return httpx.Response(200, json={"status": "1", "message": "OK", "result": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        pool = build_provider_pool(Settings(_env_file=None), client)
        response = await pool.fetch(
            ProviderRequest("ethereum", Capability.ADDRESS_HISTORY, {"address": "0xa"})
        )
    assert response.provider == "blockscout"
    assert served and "blockscout" in served[0]


async def test_a_capability_nobody_serves_is_still_explicit(http: httpx.AsyncClient) -> None:
    """The other half of graceful degradation: the fallback covers history,
    not everything, and an unserved capability must fail with zero attempts
    rather than being silently answered."""
    from cipherchain.core.errors import AllProvidersFailed
    from cipherchain.providers.base import ProviderRequest

    pool = build_provider_pool(Settings(_env_file=None), http)
    with pytest.raises(AllProvidersFailed) as exc:
        await pool.fetch(ProviderRequest("ethereum", Capability.TX_RECEIPT, {"hash": "0xa"}))
    assert exc.value.attempts == 0


async def test_the_fallback_never_outranks_a_keyed_provider(http: httpx.AsyncClient) -> None:
    """Priority, not preference: Blockscout is reached only after the keyed
    providers are spent, so a configured deployment behaves exactly as before."""
    from cipherchain.providers.base import ProviderRequest

    served: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        served.append(str(request.url))
        return httpx.Response(200, json={"status": "1", "message": "OK", "result": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        pool = build_provider_pool(Settings(_env_file=None, etherscan_api_key="k"), client)
        response = await pool.fetch(
            ProviderRequest("ethereum", Capability.ADDRESS_HISTORY, {"address": "0xa"})
        )
    assert response.provider == "etherscan"
    assert "blockscout" not in served[0]
