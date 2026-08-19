"""Investigation engine — the goal-directed loop (ENGINE_DESIGN.md, frozen).

The engine never expands blindly: every fetch pursues an objective, every
iteration is a committed checkpoint, and every end-state is an explicit
finding. It consumes the ChainRegistry, the repositories, and the
Attributor port — it has no pool reference, so vision principle 1 is
structurally enforced.
"""

from cipherchain.investigation.attribution import (
    CATEGORY_MIXER,
    CATEGORY_SANCTIONED,
    CATEGORY_VASP,
    AttributionResult,
    Attributor,
    NullAttributor,
)
from cipherchain.investigation.budgets import Budgets, BudgetTracker
from cipherchain.investigation.engine import InvestigationEngine
from cipherchain.investigation.objectives import Objective

__all__ = [
    "CATEGORY_MIXER",
    "CATEGORY_SANCTIONED",
    "CATEGORY_VASP",
    "AttributionResult",
    "Attributor",
    "BudgetTracker",
    "Budgets",
    "InvestigationEngine",
    "NullAttributor",
    "Objective",
]
