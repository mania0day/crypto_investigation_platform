"""The frozen Chain SDK contract (CHAIN_SDK_INTERFACE.md, approved 2026-08-07).

Rulings encoded here:
- D1: adapters emit BridgeHints; cross-chain resolution lives in
  ``analysis/bridges``, never in an adapter.
- D2: ``normalize()`` is async and may fetch supporting data through the
  pool (cached, provenance-tracked) — acquisition-side work stays inside
  the adapter.
- D3: ``address_history`` returns full ChainTransactions; adapters absorb
  per-chain fan-out and the pool cache dedupes.

Amended 2026-08-16 (additively, no existing field changed): a HistoryPage
also carries the feeds it could NOT read — see :class:`FeedGap`. D3 hands
the adapter a fan-out of two or three provider calls per page, and until
this field existed the loss of any one of them had exactly two possible
endings: kill the whole call, or return the survivors and say nothing. The
second is a false empty wearing the costume of an answer, so the page now
states its own gaps.

Amended 2026-09-01 (additively, no existing field or signature changed):
adapters may answer what an address HOLDS, not only what moved through it —
see :class:`BalanceSnapshot` and ``ChainAdapter.address_balance``.
``Capability.BALANCE`` already existed and was already served by two provider
clients, but nothing above the pool could reach it, so the manual explorer
could show an investigator every transfer of an account and not its balance.
The method is deliberately NOT abstract: four adapters predate it, and the
default distinguishes a declared absence (``CapabilityNotSupported`` — an
answer) from an unimplemented declaration (``NotImplementedError`` — our bug),
because serving the second as the first is how a wiring error comes to read
as a limitation of the chain.

Changing anything in this module requires the same approval as a vision
change.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from cipherchain.core.errors import CapabilityNotSupported, UnknownChain
from cipherchain.core.models import (
    Address,
    AssetBalance,
    Capability,
    Movement,
    Provenance,
    TxRef,
)


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """Optional temporal scope of a trace (vision §1: the core query)."""

    start: datetime | None = None
    end: datetime | None = None

    def __post_init__(self) -> None:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("window start must not be after end")


@dataclass(frozen=True, slots=True)
class ChainTransaction:
    """A transaction as acquired, with provenance.

    ``raw`` is opaque above the SDK — only the owning adapter's
    ``normalize()`` understands it. Everything downstream consumes
    Movements.
    """

    chain: str
    tx_hash: str
    raw: object
    provenance: Provenance


#: How a lost feed is named to a human reader. Plural throughout so the
#: sentence reads the same for every one of them ("… were unavailable"), and
#: worded as what the investigator loses rather than as the capability
#: constant: "internal_traces" is a provider word, "internal transfers
#: (contract-delivered value)" is the money that went missing from the page.
_FEED_NAMES: Mapping[Capability, str] = {
    Capability.ADDRESS_HISTORY: "native transfers",
    Capability.TOKEN_TRANSFERS: "token transfers",
    Capability.INTERNAL_TRACES: "internal transfers (contract-delivered value)",
    Capability.LOGS: "event logs",
    Capability.UTXO_LOOKUP: "unspent outputs",
}


#: The prefix every :attr:`FeedGap.code` carries. Named once so the writer and
#: the readers downstream cannot drift apart on it.
FEED_UNAVAILABLE_PREFIX = "feed_unavailable"


def feed_name_for_code(code: str) -> str:
    """The human name behind a stored ``FeedGap.code``, or the code itself.

    The gap's identity survives the adapter as a string in a database column and
    is read back much later by a report that has no Capability in hand. Without
    this the reader would either print the raw ``feed_unavailable:token_transfers``
    at a regulator, or keep a second copy of the wording that drifts from
    :data:`_FEED_NAMES` the first time one of them is edited.

    An unrecognised code comes back unchanged rather than raising: a coverage
    caveat that disappears because a capability was renamed is strictly worse
    than one that prints an ugly name.
    """
    _, _, capability = code.partition(":")
    for known, label in _FEED_NAMES.items():
        if str(known) == capability:
            return label
    return code


@dataclass(frozen=True, slots=True)
class FeedGap:
    """One acquisition feed no provider could serve for this address.

    An adapter reads an address through several feeds (an EVM page is
    ``txlist`` + ``tokentx`` + ``txlistinternal``), and losing one of them —
    an exhausted key, an open circuit, a tier that declines the capability
    outright — must cost the page that feed's rows and nothing more. That is
    the whole point: exhaustion should SLOW a trace, not stop it.

    What makes the degradation safe is this record. A run that quietly
    dropped ``tokentx`` would report "no named endpoint" for an address
    funded entirely in USDT, and every layer above would agree with it —
    the graph, the coverage counters, the document. Nothing downstream can
    detect a row that was never fetched, so the gap has to be carried out of
    the adapter as data, beside the rows that DID arrive.

    ``code`` is stable and machine-readable, matching how ``Caveat`` codes
    are consumed; ``summary`` is the sentence a reader of the report sees.
    Both are derived, so a caller cannot store one and lose the other.
    """

    chain: str
    capability: Capability
    #: Why it was lost, in the words of whatever failed (the provider error).
    #: Free text: it goes in the record for a human, never matched on.
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.chain:
            raise ValueError("a feed gap must name the chain it happened on")

    @property
    def feed(self) -> str:
        return _FEED_NAMES.get(self.capability, str(self.capability).replace("_", " "))

    @property
    def code(self) -> str:
        return f"{FEED_UNAVAILABLE_PREFIX}:{self.capability}"

    @property
    def summary(self) -> str:
        return (
            f"{self.feed} could not be read on {self.chain} for this address "
            "— no provider could serve that feed, so any value that moved "
            "only that way is missing from this page"
        )


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    """One address's holdings at one instant, and what it could not read.

    Mirrors :class:`HistoryPage` deliberately, including ``gaps``: a snapshot
    has to be honest about itself, and the rule is the same one — a holding
    missing because a feed died must never be indistinguishable from a
    holding of zero.

    ``staked`` is separate from ``native`` because on Tron they are separate
    numbers in the same payload: ``balance`` is liquid TRX only, while
    ``frozenV2`` holds what is staked. Folding them would misstate liquidity;
    omitting the second silently understates large holders, which was
    measured on a live account holding both.
    """

    address: Address
    native: AssetBalance
    staked: AssetBalance | None = None
    tokens: tuple[AssetBalance, ...] = ()
    gaps: tuple[FeedGap, ...] = ()

    @property
    def retrieved_at(self) -> datetime:
        """The OLDEST reading behind this snapshot — how stale it is at worst.

        A snapshot assembled from several calls is only as fresh as its
        laggiest part, and reporting the newest would overstate it.
        """
        readings = [self.native, *( [self.staked] if self.staked else [] ), *self.tokens]
        return min(r.provenance.retrieved_at for r in readings)

    @property
    def complete(self) -> bool:
        return not self.gaps


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One page of address history, newest first. ``next_cursor`` is an
    opaque chain-specific token; ``None`` means the history is exhausted.

    ``gaps`` is the page's honesty about itself: empty means every feed the
    adapter reads answered, and anything in it names a feed whose rows are
    absent from ``items`` for reasons that have nothing to do with the
    address. Defaulted to empty so an adapter with a single feed says
    nothing, and so no existing construction silently claims a gap it never
    had.

    ``truncated`` is the third thing that can be wrong with a page, and it
    exists because the other two cannot say it. ``next_cursor`` says "there
    is more, here is where to resume"; ``gaps`` says "a feed did not answer
    at all". Neither describes a provider that answered, stopped short of the
    end, and has no cursor to offer — which is what the keyless explorer tier
    does on Tron, where it reads numbered pages while the adapter pages by
    TronGrid fingerprint. Left unsaid, that address arrives in the report as
    one whose history simply ends, so the caveat "N address(es) had more
    history than was read" omits it. Defaulted False for the same reason
    ``gaps`` is defaulted empty: no existing construction may claim it.
    """

    items: tuple[ChainTransaction, ...]
    next_cursor: str | None
    gaps: tuple[FeedGap, ...] = ()
    truncated: bool = False

    @property
    def complete(self) -> bool:
        """Whether every feed behind this page answered. NOT about pagination
        — a full page with a ``next_cursor`` is still complete in this sense,
        and the engine already records truncation separately."""
        return not self.gaps


class BridgeDirection(enum.StrEnum):
    DEPOSIT = "deposit"  # value entering the bridge on this chain
    WITHDRAWAL = "withdrawal"  # value leaving the bridge on this chain


@dataclass(frozen=True, slots=True)
class BridgeHint:
    """Chain-local evidence that a tx touched a known bridge (ruling D1).

    Produced by ``normalize()`` from chain-specific knowledge (contract
    addresses, event signatures). Matching hints into cross-chain edges is
    ``analysis/bridges``' job, using stored normalized data from both
    chains.
    """

    bridge_id: str
    direction: BridgeDirection
    counterpart_chain: str | None
    tx: TxRef
    refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.bridge_id:
            raise ValueError("bridge_id must be non-empty")
        if not self.refs:
            raise ValueError("a bridge hint requires evidence refs")


@dataclass(frozen=True, slots=True)
class NormalizedTransaction:
    """``normalize()``'s output: canonical facts plus bridge hints."""

    tx: TxRef
    movements: tuple[Movement, ...]
    bridge_hints: tuple[BridgeHint, ...] = ()

    def __post_init__(self) -> None:
        for movement in self.movements:
            if movement.tx.chain != self.tx.chain or movement.tx.tx_hash != self.tx.tx_hash:
                raise ValueError("movement belongs to a different transaction")
        for hint in self.bridge_hints:
            if hint.tx.chain != self.tx.chain or hint.tx.tx_hash != self.tx.tx_hash:
                raise ValueError("bridge hint belongs to a different transaction")


class ChainAdapter(ABC):
    """One blockchain's semantics behind the uniform contract.

    Invariants (frozen vision doc 01):
    - Adapters are the ONLY components that reach providers, and only via
      the ProviderPool (principle 1).
    - Adapters are completely independent of each other (principle 3).
    - Everything returned upward is canonical-model data; raw payloads never
      cross the SDK boundary except opaquely inside ChainTransaction.
    """

    chain: str = ""

    @abstractmethod
    def capabilities(self) -> frozenset[Capability]:
        """Declared capabilities (principle 4). Absence is an answer."""

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities()

    def require(self, capability: Capability) -> None:
        """Raise CapabilityNotSupported for undeclared capabilities."""
        if not self.supports(capability):
            raise CapabilityNotSupported(self.chain, str(capability))

    # ── address-space knowledge (amended into the contract 2026-08-07) ──
    # Address FORMAT is per-chain knowledge, so it lives with the adapter.
    # Keeping it here is what lets the engine stay chain-agnostic and lets
    # the API detect the chain from an address alone. Defaults are safe:
    # an adapter that does not override simply never claims an address.

    def recognizes(self, address: str) -> bool:
        """Whether ``address`` belongs to this chain's address space.

        Several chains may recognize the same string (every EVM chain shares
        one address format); the registry surfaces that ambiguity rather
        than guessing.
        """
        return False

    def canonical_address(self, address: str) -> str:
        """This chain's canonical storage/lookup form for ``address``.

        Hex chains lowercase; Base58 chains must not be touched, since case
        is significant there.
        """
        return address

    @abstractmethod
    async def address_history(
        self,
        address: Address,
        *,
        window: TimeWindow | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        """Transactions touching the address, newest first, paginated."""

    async def address_balance(self, address: Address) -> BalanceSnapshot:
        """What ``address`` holds right now, with provenance.

        Deliberately NOT abstract. Four adapters predate this method, and a
        cheap balance surface is genuinely absent on some provider tiers — an
        abstract method would make every existing adapter (and the test stubs
        that implement exactly the four abstract ones) un-instantiable.

        The default separates two absences that must never collapse into one,
        because collapsing them is how a wiring bug comes to read as a chain
        limitation:

        - a chain that never declared ``Capability.BALANCE`` raises
          ``CapabilityNotSupported`` — a DECLARED absence, which is an answer;
        - a chain that declared it and never implemented this raises
          ``NotImplementedError``, loudly, because that is our bug and must
          never be served to an investigator as "this chain cannot tell you".
        """
        self.require(Capability.BALANCE)
        raise NotImplementedError(
            f"{type(self).__name__} declares {Capability.BALANCE} "
            "but implements no address_balance()"
        )

    @abstractmethod
    async def transaction(self, tx_hash: str) -> ChainTransaction:
        """Fetch one transaction. Raises ResourceNotFound if absent."""

    @abstractmethod
    async def normalize(self, tx: ChainTransaction) -> NormalizedTransaction:
        """Chain knowledge → canonical facts (ruling D2: async, may fetch)."""


class ChainRegistry:
    """Chain id → adapter. The engine resolves chains here and nowhere else."""

    def __init__(self) -> None:
        self._adapters: dict[str, ChainAdapter] = {}

    def register(self, adapter: ChainAdapter) -> None:
        if not adapter.chain:
            raise ValueError("adapter must declare its chain id")
        if adapter.chain in self._adapters:
            raise ValueError(f"duplicate adapter for chain {adapter.chain!r}")
        self._adapters[adapter.chain] = adapter

    def get(self, chain: str) -> ChainAdapter:
        try:
            return self._adapters[chain]
        except KeyError:
            raise UnknownChain(chain) from None

    def chains(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def detect(self, address: str) -> tuple[str, ...]:
        """Every registered chain whose address space claims ``address``.

        Returns all matches, in registration-name order. An empty tuple means
        no chain recognizes it; more than one means the format is genuinely
        ambiguous (e.g. any two EVM chains) and the caller must choose —
        guessing would silently trace the wrong ledger.
        """
        value = address.strip()
        return tuple(
            chain for chain in sorted(self._adapters) if self._adapters[chain].recognizes(value)
        )
