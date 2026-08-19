"""The Attributor port (ruling R1).

The engine consults attribution BEFORE spending any API budget on a node.
Implementations live in ``analysis/`` (Phase 6) and operate exclusively on
their own datasets — never on blockchain APIs (vision principle 1, Class F
boundary). Every result is a third-party claim: sourced, dated,
confidence-scored, never ground truth (vision §4).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from cipherchain.core.models import Address


class AddressRole(enum.StrEnum):
    """What an attributed address IS within its entity.

    A customer deposit address and an exchange's own collector are both "the
    exchange", and an investigator needs to tell them apart: the first names an
    ACCOUNT, the second names only the operator.
    """

    DEPOSIT = "deposit"
    OPERATIONAL = "operational"
    UNKNOWN = "unknown"


CATEGORY_VASP = "vasp"
# Known NON-custodial contracts: DEX routers, settlement contracts, protocol
# proxies, bridges. Not an accusation and not an endpoint — it exists so a
# behavioural detector cannot mistake a busy router for an exchange. A DEX
# settlement contract has the counterparty degree of a custodian and none of
# the custody.
CATEGORY_INFRASTRUCTURE = "infrastructure"
CATEGORY_SANCTIONED = "sanctioned"
CATEGORY_MIXER = "mixer"


@dataclass(frozen=True, slots=True)
class AttributionResult:
    entity: str
    category: str
    source: str
    confidence: float
    source_date: datetime | None = None
    # What KIND of address this is within the entity: a customer intake address,
    # the operator's own wallet, or unstated. Declared rather than encoded in the
    # entity string, because "Bitget (deposit address)" and "Bitget" are
    # different investigative facts and a report must not have to parse prose to
    # tell them apart. UNKNOWN is the honest default for a pack that does not say.
    role: AddressRole = AddressRole.UNKNOWN

    def __post_init__(self) -> None:
        if not self.entity or not self.category or not self.source:
            raise ValueError("attribution requires entity, category, and source")
        if not (0.0 < self.confidence < 1.0):
            raise ValueError(
                "attribution confidence must be in (0, 1) — a claim is never certainty"
            )


class Attributor(Protocol):
    async def attribute(self, address: Address) -> tuple[AttributionResult, ...]: ...


class NullAttributor:
    """No label data yet: every address is unknown. The engine then reports
    honest terminal findings instead of fabricated attributions."""

    async def attribute(self, address: Address) -> tuple[AttributionResult, ...]:
        return ()
