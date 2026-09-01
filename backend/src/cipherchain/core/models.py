"""Canonical, chain-agnostic domain model.

Everything above the chain adapters operates exclusively on these types
(vision principle 1). Nothing here may reference a specific chain, provider,
or vendor.

Design notes anchored in the frozen vision doc:

- The atomic traced fact is a :class:`Movement` — asset-aware and temporal
  (vision §6: stablecoin flows are the common case, timestamps are semantic).
- UTXO and account paradigms share one shape: account movements carry both
  endpoints; UTXO transactions decompose into input halves (``to_address is
  None``) and output halves (``from_address is None``) joined by the tx.
  Counterparty resolution is therefore data-driven and the engine never
  branches on chain identity (vision principle 5).
- Evidence is a closed taxonomy (vision §4) and is validated here so an
  ill-formed piece of evidence cannot exist anywhere in the system.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class Capability(enum.StrEnum):
    """Chain-scoped data capabilities (docs/research/CAPABILITY_MATRIX.md §1)."""

    ADDRESS_HISTORY = "address_history"
    TX_LOOKUP = "tx_lookup"
    TX_RECEIPT = "tx_receipt"
    LOGS = "logs"
    INTERNAL_TRACES = "internal_traces"
    TOKEN_TRANSFERS = "token_transfers"
    BALANCE = "balance"
    BLOCK_LOOKUP = "block_lookup"
    UTXO_LOOKUP = "utxo_lookup"


class Direction(enum.StrEnum):
    """Trace direction relative to the investigated flow."""

    BACKWARD = "backward"  # funding history — toward the previous VASP
    FORWARD = "forward"  # cash-out — toward the next VASP


class AssetKind(enum.StrEnum):
    NATIVE = "native"
    TOKEN = "token"


@dataclass(frozen=True, slots=True)
class Asset:
    """An asset on one chain. Token assets require a contract; native forbid it."""

    chain: str
    kind: AssetKind
    symbol: str
    decimals: int
    contract: str | None = None

    def __post_init__(self) -> None:
        if self.decimals < 0:
            raise ValueError(f"decimals must be >= 0, got {self.decimals}")
        if self.kind is AssetKind.TOKEN and not self.contract:
            raise ValueError("token assets require a contract address")
        if self.kind is AssetKind.NATIVE and self.contract is not None:
            raise ValueError("native assets must not carry a contract address")


@dataclass(frozen=True, slots=True)
class Address:
    """A chain-qualified address in the adapter's canonical form."""

    chain: str
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("address value must be non-empty")


@dataclass(frozen=True, slots=True)
class TxRef:
    """A chain-qualified transaction reference with its temporal anchor."""

    chain: str
    tx_hash: str
    timestamp: datetime
    block_number: int | None = None

    def __post_init__(self) -> None:
        if not self.tx_hash:
            raise ValueError("tx_hash must be non-empty")


class MovementKind(enum.StrEnum):
    NATIVE = "native"  # account-model native value
    TOKEN = "token"  # token transfer
    INTERNAL = "internal"  # EVM internal (trace-derived) value move
    UTXO_INPUT = "utxo_input"  # value entering a UTXO tx (to_address is None)
    UTXO_OUTPUT = "utxo_output"  # value leaving a UTXO tx (from_address is None)


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a fact came from — provider, when, and the raw payload's digest.

    ``payload_sha256`` is the content address of the raw provider response
    backing this fact, so any derived finding can be replayed and
    independently verified (vision §4, evidence provenance).
    """

    provider: str
    retrieved_at: datetime
    payload_sha256: str

    def __post_init__(self) -> None:
        if len(self.payload_sha256) != 64:
            raise ValueError("payload_sha256 must be a hex sha256 digest (64 chars)")


@dataclass(frozen=True, slots=True)
class AssetBalance:
    """How much of one asset an address holds, with the reading's provenance.

    ``amount`` is in the asset's smallest unit as an int, exactly as
    :class:`Movement` — a balance and a movement are the same kind of
    quantity and must not become two kinds of number. A float here would be
    the same silent-rounding bug value accounting refuses everywhere else.

    Carries :class:`Provenance` because a balance is a reading taken at an
    instant, not a standing fact: which provider answered, and when, is the
    difference between a number an investigator can cite and one they cannot.
    """

    asset: Asset
    amount: int
    provenance: Provenance

    def __post_init__(self) -> None:
        # bool is an int subclass; excluded so True cannot pass as 1.
        if not isinstance(self.amount, int) or isinstance(self.amount, bool):
            raise ValueError("amount must be an int in the asset's smallest unit")
        if self.amount < 0:
            raise ValueError(f"amount must be >= 0, got {self.amount}")


@dataclass(frozen=True, slots=True)
class Movement:
    """One canonical value movement — the atomic fact the engine traces.

    ``amount`` is always in the asset's smallest unit (wei / sats / lamports)
    as a Python int; floats never appear in value accounting.

    ``gas_price`` is the same kind of quantity for the fee the sender paid per
    unit of gas, and is the one field here that exists to serve a single
    consumer: the unique-gas-price rung of the mixer exit ladder, which links a
    deposit to a withdrawal by EXACT equality of a hand-set price. It hangs off
    the movement rather than off :class:`TxRef` because a movement is what both
    the ladder and the fact store hold (``movements.gas_price``). WHICH field of
    a transaction the number is copied from is a per-chain judgement and belongs
    to the adapter that makes it, recorded there.
    """

    tx: TxRef
    asset: Asset
    amount: int
    kind: MovementKind
    from_address: Address | None
    to_address: Address | None
    index: int
    provenance: Provenance
    # Vantage-stable identity within the transaction. Two acquisitions of the
    # same logical movement must produce the same key (so re-normalization
    # dedups instead of dropping/duplicating rows — REVIEW_FINDINGS.md #1);
    # distinct movements must differ. Adapters set it from intrinsic structure
    # (UTXO vin/vout position, token endpoints+contract). Defaults to the
    # positional index, which is only safe when the full tx is always visible.
    dedup_key: str | None = None
    # Fee price per unit of gas, in the chain's smallest unit, or None where
    # the acquisition never reported one.
    #
    # OPTIONAL WITH A DEFAULT, deliberately. Most chains have no gas price at
    # all (UTXO chains price a whole transaction, not a unit of computation),
    # and every adapter and test that builds a movement predates this field. A
    # required field would force those callers to supply something, and the
    # something they would supply is 0 — which is not "unknown", it is a real
    # gas price. Two unknowns rendered as 0 compare EQUAL, and the rung that
    # reads this field would then link a deposit to an arbitrary withdrawal and
    # publish the pairing as evidence. None and 0 must never collapse into each
    # other; the default keeps absence expressible.
    gas_price: int | None = None

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("amount must be >= 0")
        if not isinstance(self.amount, int) or isinstance(self.amount, bool):
            raise ValueError("amount must be an int in the asset's smallest unit")
        if self.gas_price is not None:
            # Same stance as amount, and one reason beyond it: the fact store
            # refuses a negative gas price (CHECK gas_price >= 0), so a parse
            # bug that survived to here would abort a whole insert batch mid-run
            # instead of failing at the adapter that produced it. Caught here,
            # the movement that is wrong is the one that raises.
            if not isinstance(self.gas_price, int) or isinstance(self.gas_price, bool):
                raise ValueError("gas_price must be an int in the chain's smallest unit")
            if self.gas_price < 0:
                raise ValueError("gas_price must be >= 0")
        if self.index < 0:
            raise ValueError("index must be >= 0")
        if self.from_address is None and self.to_address is None:
            raise ValueError("a movement needs at least one endpoint")
        if self.kind is MovementKind.UTXO_INPUT and (
            self.from_address is None or self.to_address is not None
        ):
            raise ValueError("utxo_input must have from_address set and to_address None")
        if self.kind is MovementKind.UTXO_OUTPUT and (
            self.to_address is None or self.from_address is not None
        ):
            raise ValueError("utxo_output must have to_address set and from_address None")
        if self.kind in (MovementKind.NATIVE, MovementKind.TOKEN, MovementKind.INTERNAL) and (
            self.from_address is None or self.to_address is None
        ):
            raise ValueError(f"{self.kind} movements must have both endpoints")
        for endpoint in (self.from_address, self.to_address):
            if endpoint is not None and endpoint.chain != self.tx.chain:
                raise ValueError(f"endpoint chain {endpoint.chain!r} != tx chain {self.tx.chain!r}")
        if self.asset.chain != self.tx.chain:
            raise ValueError(f"asset chain {self.asset.chain!r} != tx chain {self.tx.chain!r}")


class EvidenceKind(enum.StrEnum):
    """The closed evidence taxonomy (vision §4) — never conflated."""

    ONCHAIN_FACT = "onchain_fact"
    HEURISTIC_INFERENCE = "heuristic_inference"
    THIRD_PARTY_CLAIM = "third_party_claim"
    # A statement about CipherChain's own run — what it examined, where it stopped,
    # what it never looked at. Verifiable against the investigation record
    # rather than against the chain, which is precisely why it may not wear the
    # onchain_fact stamp. Added by Ruling 4 (NEXT_MILESTONE_DECISIONS.md); it
    # NARROWS what onchain_fact means rather than widening the taxonomy's reach.
    ENGINE_OBSERVATION = "engine_observation"


@dataclass(frozen=True, slots=True)
class Evidence:
    """One piece of support for a finding, valid per its taxonomy kind.

    - ``ONCHAIN_FACT``: verifiable by anyone; carries refs, never a confidence.
    - ``HEURISTIC_INFERENCE``: requires ``heuristic`` ("name@version") and a
      confidence in (0, 1].
    - ``THIRD_PARTY_CLAIM``: requires ``source`` (+ its date when known) and a
      confidence in (0, 1].
    - ``ENGINE_OBSERVATION``: a statement about this run. Never carries a
      confidence — the engine is not guessing about its own behaviour — and
      never a source or heuristic. Refs are optional, because the honest
      statement sometimes concerns what was NOT examined.
    """

    kind: EvidenceKind
    summary: str
    refs: tuple[str, ...] = ()
    source: str | None = None
    source_date: datetime | None = None
    heuristic: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.summary:
            raise ValueError("evidence must carry a human-readable summary")
        if self.confidence is not None and not (0.0 < self.confidence <= 1.0):
            raise ValueError("confidence must be in (0, 1]")
        match self.kind:
            case EvidenceKind.ONCHAIN_FACT:
                if not self.refs:
                    raise ValueError("onchain_fact evidence requires refs (tx hashes/digests)")
                if self.confidence is not None:
                    raise ValueError("onchain_fact evidence must not carry a confidence")
                if self.heuristic is not None or self.source is not None:
                    raise ValueError("onchain_fact evidence carries neither heuristic nor source")
            case EvidenceKind.HEURISTIC_INFERENCE:
                if not self.heuristic or "@" not in self.heuristic:
                    raise ValueError("inference evidence requires heuristic 'name@version'")
                if self.confidence is None:
                    raise ValueError("inference evidence requires a confidence")
                if self.confidence >= 1.0:
                    raise ValueError("an inference is never certainty (confidence < 1.0)")
                # An inference has no external source. If one informed it, that
                # source is its OWN piece of evidence — a reader must be able to
                # weigh the claim and the reasoning separately.
                if self.source is not None:
                    raise ValueError(
                        "inference evidence carries no source — cite the claim it rests on "
                        "as a separate piece of evidence"
                    )
            case EvidenceKind.THIRD_PARTY_CLAIM:
                if not self.source:
                    raise ValueError("claim evidence requires a source")
                if self.confidence is None:
                    raise ValueError("claim evidence requires a confidence")
                if self.confidence >= 1.0:
                    raise ValueError("a third-party claim is never certainty (confidence < 1.0)")
                # The laundering guard: without this an inference can wear claim
                # clothes with its heuristic still attached, and every consumer
                # that switches on `.kind` reads it as sourced attribution.
                if self.heuristic is not None:
                    raise ValueError(
                        "a third-party claim is not a heuristic inference — "
                        "an inference derived from a claim is separate evidence"
                    )
            case EvidenceKind.ENGINE_OBSERVATION:
                if self.confidence is not None:
                    raise ValueError("engine_observation evidence must not carry a confidence")
                if self.heuristic is not None or self.source is not None:
                    raise ValueError(
                        "engine_observation evidence carries neither heuristic nor source"
                    )


class FindingKind(enum.StrEnum):
    VASP_ENDPOINT = "vasp_endpoint"
    SANCTIONED_ADDRESS = "sanctioned_address"
    MIXER_INTERACTION = "mixer_interaction"
    BRIDGE_CROSSING = "bridge_crossing"
    SWEEP_PATTERN = "sweep_pattern"
    # Structural laundering shapes other than pass-through. Kept distinct
    # from SWEEP_PATTERN so a report does not label a peel chain or a
    # splitter "sweep"; the evidence's versioned heuristic names which one.
    OBFUSCATION_PATTERN = "obfuscation_pattern"
    TERMINAL = "terminal"  # explicit dead-end — an answer, not a failure (vision §1)


@dataclass(frozen=True, slots=True)
class Finding:
    """An evidence-backed conclusion. Findings without evidence cannot exist
    (vision principle 7)."""

    kind: FindingKind
    subject: Address
    summary: str
    confidence: float
    evidence: tuple[Evidence, ...]
    direction: Direction | None = None

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError("a finding requires at least one piece of evidence")
        if not (0.0 < self.confidence <= 1.0):
            raise ValueError("confidence must be in (0, 1]")
        if not self.summary:
            raise ValueError("a finding requires a summary")
