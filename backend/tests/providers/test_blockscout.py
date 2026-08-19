"""The keyless fallback tier.

Reached only when the keyed providers are spent, so its job is to keep a
trace moving rather than to be fast. What it must NOT do is quietly discard
real movements, which is what the strict Etherscan envelope reading would do
to Blockscout's partial-success status.
"""

import httpx
import pytest

from cipherchain.core.errors import ProviderRateLimited, ProviderResponseInvalid
from cipherchain.core.models import Capability
from cipherchain.providers.base import ProviderRequest
from cipherchain.providers.clients import BlockscoutProvider

ROW = {
    "blockNumber": "25745168",
    "timeStamp": "1755000000",
    "hash": "0xab40",
    "from": "0x28c6",
    "to": "0x50de",
    "value": "1000",
    "isError": "0",
}


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def request(capability: Capability = Capability.ADDRESS_HISTORY) -> ProviderRequest:
    return ProviderRequest(chain="ethereum", capability=capability, params={"address": "0x28c6"})


class TestSupport:
    def test_serves_only_configured_chains_and_capabilities(self) -> None:
        provider = BlockscoutProvider(client(lambda r: httpx.Response(200)))
        assert provider.supports("ethereum", Capability.ADDRESS_HISTORY)
        assert provider.supports("polygon", Capability.TOKEN_TRANSFERS)
        assert provider.supports("ethereum", Capability.INTERNAL_TRACES)
        assert not provider.supports("bitcoin", Capability.ADDRESS_HISTORY)
        assert not provider.supports("ethereum", Capability.TX_RECEIPT)

    async def test_the_chain_is_chosen_by_host_not_by_a_parameter(self) -> None:
        """Blockscout runs one instance per chain, unlike Etherscan V2."""
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(str(req.url))
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": [ROW]})

        provider = BlockscoutProvider(client(handler))
        await provider.execute(
            ProviderRequest(
                chain="polygon",
                capability=Capability.ADDRESS_HISTORY,
                params={"address": "0x1"},
            )
        )
        assert "polygon.blockscout.com" in seen[0]
        assert "chainid" not in seen[0]

    async def test_it_identifies_itself_rather_than_posing_as_a_browser(self) -> None:
        seen: dict[str, str] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen.update(req.headers)
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": []})

        await BlockscoutProvider(client(handler)).execute(request())
        assert "CipherChain" in seen["user-agent"]

    async def test_no_credential_is_ever_sent(self) -> None:
        """There is none — so nothing can leak into a cache key or the store."""
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(str(req.url))
            return httpx.Response(200, json={"status": "1", "message": "OK", "result": []})

        await BlockscoutProvider(client(handler)).execute(request())
        assert "apikey" not in seen[0].lower()


class TestEnvelope:
    async def test_a_normal_answer_returns_its_rows(self) -> None:
        provider = BlockscoutProvider(
            client(lambda r: httpx.Response(200, json={"status": "1", "result": [ROW]}))
        )
        response = await provider.execute(request())
        assert response.payload == [ROW]
        assert response.provider == "blockscout"
        assert len(response.payload_sha256) == 64

    async def test_partial_status_keeps_its_rows(self) -> None:
        """Status "2" is Blockscout saying "these are real, there may be more".

        Live on txlistinternal (2026-08-13). Reading it as an error — which the
        strict Etherscan rule does — throws away movements the chain really
        contains, and contract-delivered value is exactly what that feed exists
        to surface.
        """
        body = {
            "status": "2",
            "message": "Some internal transactions within this block range have not yet "
            "been processed",
            "result": [ROW],
        }
        provider = BlockscoutProvider(client(lambda r: httpx.Response(200, json=body)))
        response = await provider.execute(request(Capability.INTERNAL_TRACES))
        assert response.payload == [ROW], "a partial answer must not be discarded"

    async def test_internal_traces_get_the_hash_key_the_adapter_expects(self) -> None:
        """Blockscout spells it `transactionHash`; every other EVM feed says
        `hash`. A live call raised KeyError: 'hash' inside the adapter on the
        first internal-trace fetch — mocks built from the Etherscan shape could
        not have caught it. Repaired in the client so the layers above the pool
        never learn which vendor answered.
        """
        row = {"blockNumber": "1", "timeStamp": "2", "transactionHash": "0xdead", "from": "0xa"}
        body = {"status": "1", "message": "OK", "result": [row]}
        provider = BlockscoutProvider(client(lambda r: httpx.Response(200, json=body)))
        response = await provider.execute(request(Capability.INTERNAL_TRACES))
        assert response.payload[0]["hash"] == "0xdead"
        # The vendor's own spelling survives too — we add, never rewrite.
        assert response.payload[0]["transactionHash"] == "0xdead"

    async def test_the_digest_still_covers_the_untouched_vendor_bytes(self) -> None:
        """Aligning the payload must not change what the provenance digest
        attests to, or a cited hash stops verifying against what was sent."""
        from cipherchain.core.hashing import sha256_hex

        body = {"status": "1", "result": [{"transactionHash": "0xdead", "blockNumber": "1"}]}
        captured: dict[str, bytes] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            response = httpx.Response(200, json=body)
            captured["raw"] = response.content
            return response

        provider = BlockscoutProvider(client(handler))
        response = await provider.execute(request(Capability.INTERNAL_TRACES))
        assert response.payload_sha256 == sha256_hex(captured["raw"])

    async def test_benign_emptiness_is_an_empty_list_not_an_error(self) -> None:
        """An address with no history is an answer, not a failure — otherwise
        the pool fails over and burns every remaining provider on it."""
        body = {"status": "0", "message": "No transactions found", "result": []}
        provider = BlockscoutProvider(client(lambda r: httpx.Response(200, json=body)))
        assert (await provider.execute(request())).payload == []

    async def test_rate_limiting_is_raised_as_rate_limiting(self) -> None:
        body = {"status": "0", "message": "Max rate limit reached", "result": ""}
        provider = BlockscoutProvider(client(lambda r: httpx.Response(200, json=body)))
        with pytest.raises(ProviderRateLimited):
            await provider.execute(request())

    async def test_a_non_json_body_is_invalid_not_empty(self) -> None:
        """An HTML challenge page must never read as 'this address is clean'."""
        provider = BlockscoutProvider(
            client(lambda r: httpx.Response(200, text="<html>challenge</html>"))
        )
        with pytest.raises(ProviderResponseInvalid):
            await provider.execute(request())

    async def test_a_missing_envelope_is_refused(self) -> None:
        provider = BlockscoutProvider(client(lambda r: httpx.Response(200, json={"nope": 1})))
        with pytest.raises(ProviderResponseInvalid):
            await provider.execute(request())
