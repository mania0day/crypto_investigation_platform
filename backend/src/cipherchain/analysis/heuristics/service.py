"""Service-endpoint detection — identifying a VASP *without* a label.

The chain never records who owns an address, so no amount of analysis can
tell you a wallet is "Binance". But it does record **behaviour**, and
exchange infrastructure behaves unmistakably: it collects from very many
distinct addresses, pays out to very many distinct addresses, and does both
continuously.

That distinction is the whole point of this module:

- **Role** ("this is a service/exchange endpoint") — inferable from the
  chain, and usually enough for an investigator, who can subpoena the
  address once they know what it is.
- **Identity** ("this is *Acme* Exchange") — off-chain information. Only a
  sourced label can supply it, and CipherChain never guesses it.

So this emits a VASP endpoint finding whose evidence is a versioned
HEURISTIC_INFERENCE and which explicitly calls the operator *unnamed*. A
labelled attribution, when one exists, is strictly better and takes
precedence in the engine.

False-positive discipline: token airdropper contracts and NFT mints also
touch thousands of addresses. They are excluded by requiring the traffic to
be **bidirectional** — a service takes money in *and* pays money out, while
a distributor only pays out. Both thresholds are stated in the evidence.
"""

from __future__ import annotations

from collections.abc import Sequence

from cipherchain.core.models import (
    Address,
    Evidence,
    EvidenceKind,
    Finding,
    FindingKind,
)
from cipherchain.storage.repositories import StoredMovement

SERVICE_HEURISTIC = "service-endpoint@1"

# A wallet must look like infrastructure on BOTH sides. Retail wallets and
# even busy traders rarely sustain this in two directions at once.
MIN_SENDERS = 25
MIN_RECIPIENTS = 25
MIN_TOTAL_COUNTERPARTIES = 60


def _distinct(movements: Sequence[StoredMovement], attr: str) -> set[int]:
    return {getattr(m, attr) for m in movements if getattr(m, attr) is not None}


def is_sentinel_address(value: str) -> bool:
    """All-zero addresses are burn/null sentinels, not operators.

    Every chain uses one, and it accumulates traffic from everywhere — so it
    trips any degree-based test while belonging to nobody. Calling it a
    service endpoint would end a trace at a black hole.
    """
    body = value[2:] if value[:2].lower() == "0x" else value
    return bool(body) and set(body) <= {"0", "x"}


def meets_service_thresholds(senders: int, recipients: int) -> bool:
    """Does this counterparty degree look like custodial infrastructure?

    Extracted so the manual explorer can apply the SAME rule to the
    counterparties it reads live, instead of growing a second opinion about
    what a service endpoint is that would drift from this one.
    """
    return (
        senders >= MIN_SENDERS
        and recipients >= MIN_RECIPIENTS
        and senders + recipients >= MIN_TOTAL_COUNTERPARTIES
    )


def service_confidence(senders: int, recipients: int) -> float:
    """Confidence for a service-endpoint inference of this degree.

    Grows with scale but is capped well below certainty: this is a
    behavioural inference about an UNNAMED operator, not an attribution.
    Shared with the manual explorer for the same anti-drift reason as
    :func:`meets_service_thresholds`.
    """
    total = senders + recipients
    return round(min(0.55 + 0.20 * min(total / 400, 1.0), 0.75), 3)


def assess_service_endpoint(
    incoming: Sequence[StoredMovement],
    outgoing: Sequence[StoredMovement],
) -> tuple[bool, int, int]:
    """Return (looks_like_a_service, senders, recipients)."""
    senders = len(_distinct(incoming, "from_address_id"))
    recipients = len(_distinct(outgoing, "to_address_id"))
    return meets_service_thresholds(senders, recipients), senders, recipients


def detect_service_endpoint(
    address: Address,
    incoming: Sequence[StoredMovement],
    outgoing: Sequence[StoredMovement],
) -> list[Finding]:
    """One finding when an address behaves like custodial infrastructure."""
    if is_sentinel_address(address.value):
        return []
    looks_like, senders, recipients = assess_service_endpoint(incoming, outgoing)
    if not looks_like:
        return []

    total = senders + recipients
    confidence = service_confidence(senders, recipients)
    refs = tuple(sorted({m.tx_hash for m in list(incoming) + list(outgoing)}))[:8]

    return [
        Finding(
            kind=FindingKind.VASP_ENDPOINT,
            subject=address,
            summary=(
                f"service endpoint (operator unnamed): collects from {senders} and "
                f"pays out to {recipients} distinct addresses — behaves as custodial "
                f"infrastructure such as an exchange"
            ),
            confidence=confidence,
            evidence=(
                Evidence(
                    kind=EvidenceKind.ONCHAIN_FACT,
                    summary=(
                        f"{len(incoming)} inbound and {len(outgoing)} outbound movements "
                        f"across {total} distinct counterparties"
                    ),
                    refs=refs,
                ),
                Evidence(
                    kind=EvidenceKind.HEURISTIC_INFERENCE,
                    summary=(
                        f"bidirectional counterparty degree ({senders} in / {recipients} out) "
                        f"exceeds the service thresholds ({MIN_SENDERS}/{MIN_RECIPIENTS}); "
                        f"identifies the ROLE only — the operator's identity is off-chain "
                        f"and requires a sourced label"
                    ),
                    heuristic=SERVICE_HEURISTIC,
                    confidence=confidence,
                    refs=refs,
                ),
            ),
        )
    ]
