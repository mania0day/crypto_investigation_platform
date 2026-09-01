"""USD prices — a market claim, never a chain fact.

A balance is something the ledger says; a price is something a market said at
an instant, and the two decay at completely different rates. That difference
is why this is a module beside :mod:`cipherchain.intel.explorer_tags` rather
than a capability behind the provider pool:

- ``ProviderRequest.chain`` is required and part of the pool's cache key, but
  "what is TRX worth" is not a fact about the Tron ledger. Filing it there
  means inventing a chain id and publishing per-chain metrics that lie.
- the pool's TTL is one global setting shared by every cached capability,
  while a price wants ~60s and ``Capability.BALANCE`` must stay
  ``CachePolicy.NEVER``. One knob cannot express both.
- ``ProviderPool.fetch`` raises ``AllProvidersFailed``. "Failure is always
  soft" for a price means the caller never sees an exception at all: an
  investigator who cannot get a dollar figure has still been told the
  balance, and that is the number that matters.

The one rule this module exists to enforce: **a price never travels without
its source and its timestamp**. A dollar figure with no "as of" is a claim
nobody can check, which is exactly what this codebase refuses everywhere
else (see the ``source_date`` discipline through the intel lifecycle).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)

__all__ = ["USD_SOURCE", "PriceFeed", "PriceQuote", "value_usd"]

#: Marks every quote this module produces, so a stored or rendered number can
#: always be traced back to who said it.
USD_SOURCE = "coingecko-simple-price"
SOURCE_URL = "https://www.coingecko.com/en/api"

_ENDPOINT = "https://api.coingecko.com/api/v3/simple/price"

#: How long a quote is served before a refresh is attempted. Short, because a
#: price is live state; not zero, because an investigator clicking through a
#: graph must not turn into one request per click against a free public API.
QUOTE_TTL_SECONDS = 60.0

#: How long a STALE quote may still be served after a refresh fails. Honest
#: rather than sloppy precisely because ``retrieved_at`` travels with the
#: number and the API marks it ``stale`` — a blank panel every time a free
#: service hiccups is worse than a number that says how old it is.
STALE_GRACE_SECONDS = 900.0

REQUEST_TIMEOUT_SECONDS = 10.0

#: symbol -> CoinGecko coin id. A TABLE, never a guess: an inferred id prices
#: one asset with a different asset's market and nothing downstream could tell.
#: Same posture as assets/, labels/ and bridges/ — nothing ships unverified.
COIN_IDS: Mapping[str, str] = {
    "TRX": "tron",
    "ETH": "ethereum",
    "SOL": "solana",
    "BTC": "bitcoin",
    "USDT": "tether",
    "USDC": "usd-coin",
    "DAI": "dai",
    "WETH": "weth",
    "MATIC": "matic-network",
    "POL": "polygon-ecosystem-token",
}


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """One asset's USD price, with everything needed to judge it.

    ``quoted_at`` is the MARKET's own last-updated time and ``retrieved_at``
    is when we asked — deliberately separate, because a feed that has stopped
    updating still answers instantly, and only the first of those two numbers
    reveals it.
    """

    symbol: str
    usd: Decimal
    source: str
    source_url: str
    quoted_at: datetime | None
    retrieved_at: datetime
    stale: bool = False


def value_usd(amount: int, decimals: int, quote: PriceQuote) -> Decimal:
    """Convert a smallest-unit amount to USD.

    ``Decimal`` throughout: a WETH balance in wei exceeds 2^53, and this
    codebase already refuses JSON numbers for smallest-unit values for that
    reason. Doing the multiply in float would reintroduce the same rounding
    at the last step.
    """
    return (Decimal(amount) / (Decimal(10) ** decimals)) * quote.usd


class PriceFeed:
    """Cached, self-pacing, never-raising USD quotes.

    One batched request covers every id in :data:`COIN_IDS`, so the ceiling
    is one request per TTL no matter how many symbols a caller asks for or
    how fast an investigator clicks — the same "stay welcome at a free public
    API" argument ``explorer_tags.REQUEST_SPACING_SECONDS`` makes.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = QUOTE_TTL_SECONDS,
        stale_grace_seconds: float = STALE_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        base_url: str = _ENDPOINT,
        http: httpx.AsyncClient | None = None,
        api_key: str | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._grace = stale_grace_seconds
        # Injected so TTL expiry is testable deterministically and instantly,
        # the same shape ProviderPool.__init__ already establishes.
        self._clock = clock
        self._now = now
        self._base_url = base_url
        self._http = http
        self._api_key = api_key
        self._quotes: dict[str, PriceQuote] = {}
        self._fetched_at: float | None = None
        # Single-flight: N panels opening at once produce ONE request.
        self._lock = asyncio.Lock()

    async def quotes(self, symbols: Iterable[str]) -> dict[str, PriceQuote]:
        """Quotes for ``symbols``. Never raises; unknown symbols are absent.

        An absent symbol is the honest representation of "no usable price" —
        the caller renders the balance without a dollar figure rather than
        printing a zero that would read as "worthless".
        """
        wanted = {s.upper() for s in symbols}
        if not wanted:
            return {}
        async with self._lock:
            await self._refresh_if_stale()
        out: dict[str, PriceQuote] = {}
        for symbol in wanted:
            quote = self._quotes.get(symbol)
            if quote is not None:
                out[symbol] = quote
        return out

    async def _refresh_if_stale(self) -> None:
        age = None if self._fetched_at is None else self._clock() - self._fetched_at
        if age is not None and age < self._ttl:
            return
        try:
            fetched = await self._fetch()
        except Exception as exc:
            # Serve what we have, marked stale, while it is inside the grace
            # window; beyond that, serve nothing rather than a number too old
            # to stand behind.
            logger.debug("price refresh failed (%s); serving cache", exc)
            if age is not None and age > self._grace:
                self._quotes = {}
            else:
                self._quotes = {s: _as_stale(q) for s, q in self._quotes.items()}
            return
        self._quotes = fetched
        self._fetched_at = self._clock()

    async def _fetch(self) -> dict[str, PriceQuote]:
        ids = sorted(set(COIN_IDS.values()))
        params = {
            "ids": ",".join(ids),
            "vs_currencies": "usd",
            # The market's own quote time — the honest source_date for the
            # number, and the only way to notice a feed that has frozen.
            "include_last_updated_at": "true",
        }
        headers = {"x-cg-demo-api-key": self._api_key} if self._api_key else {}
        if self._http is not None:
            response = await self._http.get(
                self._base_url, params=params, headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        else:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as http:
                response = await http.get(self._base_url, params=params, headers=headers)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("price feed returned a non-object body")

        retrieved_at = self._now()
        by_id = {coin_id: symbol for symbol, coin_id in COIN_IDS.items()}
        out: dict[str, PriceQuote] = {}
        for coin_id, entry in body.items():
            symbol = by_id.get(str(coin_id))
            if symbol is None or not isinstance(entry, dict):
                continue
            usd = entry.get("usd")
            if usd is None:
                continue
            try:
                price = Decimal(str(usd))
            except (ArithmeticError, ValueError):
                continue
            if price <= 0:
                continue  # a non-positive price is not a price
            updated = entry.get("last_updated_at")
            out[symbol] = PriceQuote(
                symbol=symbol,
                usd=price,
                source=USD_SOURCE,
                source_url=SOURCE_URL,
                quoted_at=(
                    datetime.fromtimestamp(int(updated), tz=UTC)
                    if isinstance(updated, int | float)
                    else None
                ),
                retrieved_at=retrieved_at,
            )
        return out


def _as_stale(quote: PriceQuote) -> PriceQuote:
    """The same quote, flagged. ``retrieved_at`` is NOT refreshed — the whole
    point is that the number is as old as it says it is."""
    if quote.stale:
        return quote
    return PriceQuote(
        symbol=quote.symbol,
        usd=quote.usd,
        source=quote.source,
        source_url=quote.source_url,
        quoted_at=quote.quoted_at,
        retrieved_at=quote.retrieved_at,
        stale=True,
    )
