"""The four investigation budgets, their tracker, and the pursuit that raises them.

Budgets protect the investigation from supernodes and runaway expansion;
provider rate limits (pool layer) protect the quotas. Both exist, at
different layers (CAPABILITY_MATRIX.md §9).

`api_calls` charges one unit per **address expansion**, not per upstream
provider call — a stated v1 approximation. One expansion currently costs
three upstream calls on the EVM family (txlist + tokentx + txlistinternal),
two on Tron, and one elsewhere, so the unit is a traversal bound rather than
a quota bound. Quota is protected at a different layer, by the pool's rate
limiter (CAPABILITY_MATRIX.md §9); exact per-investigation pool accounting
arrives when the pool gains request tagging. `seconds` is a per-run wall
clock: resuming an investigation grants a fresh time budget but keeps
everything else spent.

Pursuit: a budget is a cost, not an answer
------------------------------------------
The instruction was given repeatedly — *"i dont want to stop at mixer i want
it to go foward until VASP … VASP will be there, there is no way no VASP"*,
then *"report has no VASP and i told you dont stop until VASP??"*. On the
shipped case the run stopped at 400 nodes with the FORWARD objective
unanswered and 382 addresses still on the frontier, and only a hand-issued
``POST /investigations/{id}/resume`` with a bigger budget found OKX. The
answer was reachable; the run simply stopped asking, and a human had to do by
hand what the loop could have decided for itself.

So an exhausted budget is now a decision point rather than a terminal.
``pursue_until_answered`` (default ON — the user's ruling) lets the engine
grant itself another allowance while an objective still has no NAMED endpoint
and the frontier still holds work. Three properties keep that from becoming
the runaway the budgets were built to prevent:

1. **``max_depth`` is never extended.** It is the only budget that is not a
   cost: it decides what the trace MEANS. The README already tells operators
   that raising it does not reopen depth-horizon branches and that going
   deeper is a NEW investigation. Extending it here would silently rewrite the
   question the record says was asked, so it is absent from
   ``EXTENDABLE_BUDGETS`` and ``extend`` refuses it by name.
2. **There is always a ceiling** — ``max_extensions``, counted across the
   whole investigation rather than per run, because a resume that reset the
   count would be an unbounded loop with extra steps.
3. **Every extension is a recorded fact.** ``BudgetExtension`` is written into
   the investigation's ``spent`` record as it is granted and read back by the
   report, so an investigator can see that the run spent several times its
   stated allowance chasing an answer, and on which budget.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

#: The budgets pursuit may raise. ``max_depth`` is deliberately absent — see
#: property 1 in the module docstring. Anything added here must be a COST;
#: anything that changes the meaning of the trace belongs to the operator.
EXTENDABLE_BUDGETS: Final[tuple[str, ...]] = ("api_calls", "seconds", "max_nodes")


def _amount(value: float) -> str:
    """Whole numbers print whole. ``api_calls`` and ``max_nodes`` are counts and
    ``100.0 -> 200.0`` in a law-enforcement document reads like a rounding
    artefact rather than a limit somebody chose."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


@dataclass(frozen=True, slots=True)
class BudgetExtension:
    """One grant of extra allowance, and why it was granted.

    A record rather than a log line: the log is gone by the time anybody reads
    the report, and "this run cost nine times what its budget says" is exactly
    the kind of fact a defence would rather find missing. Carries the objectives
    that were still unanswered at the moment of the grant, because that — not
    the number — is the justification.
    """

    budget: str
    previous: float
    granted: float
    unanswered: tuple[str, ...]

    def statement(self) -> str:
        """The one wording of this fact, used by the engine's evidence AND by the
        report's caveat. Two spellings would eventually disagree about the same
        grant, and the report is the copy nobody can cross-check against."""
        objectives = ", ".join(self.unanswered) or "no objective named"
        return (
            f"budget '{self.budget}' extended from {_amount(self.previous)} to "
            f"{_amount(self.granted)} to keep pursuing an unanswered objective "
            f"({objectives} still had no named endpoint)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget,
            "previous": self.previous,
            "granted": self.granted,
            "unanswered": list(self.unanswered),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BudgetExtension:
        unanswered = raw.get("unanswered") or ()
        return cls(
            budget=str(raw.get("budget", "unknown")),
            previous=float(raw.get("previous", 0)),
            granted=float(raw.get("granted", 0)),
            unanswered=tuple(str(o) for o in unanswered),
        )


def extension_summary(extensions: Sequence[BudgetExtension]) -> str:
    """First limit to last, per budget: ``api_calls 100 → 900``.

    The engine's coverage statement is one sentence carried by every finding a
    run files, so eight full ``BudgetExtension.statement()`` lines cannot go in
    it. This is the compressed form — what the ceiling ended up being, per
    budget — while the per-grant statements stay available to the report, which
    has room to list them.
    """
    span: dict[str, tuple[float, float]] = {}
    for extension in extensions:
        first = span.get(extension.budget, (extension.previous, extension.granted))[0]
        span[extension.budget] = (first, extension.granted)
    return ", ".join(
        f"{name} {_amount(low)} → {_amount(high)}" for name, (low, high) in span.items()
    )


@dataclass(frozen=True, slots=True)
class Budgets:
    api_calls: int = 100
    seconds: float = 300.0
    max_depth: int = 6
    max_nodes: int = 500
    #: Keep going when a COST budget runs out and an objective is still
    #: unanswered. Default ON by ruling: the run that stops holding 382
    #: unexplored addresses and no named endpoint has not answered the
    #: question it was asked, it has only stopped paying for it.
    pursue_until_answered: bool = True
    #: The ceiling on that pursuit, across the whole investigation. Not
    #: optional and not unbounded: "keep going until you find one" spent
    #: overnight against a paid quota is a runaway, not diligence.
    max_extensions: int = 8

    def __post_init__(self) -> None:
        if min(self.api_calls, self.max_depth, self.max_nodes) < 1 or self.seconds <= 0:
            raise ValueError("all budgets must be positive")
        if self.max_extensions < 0:
            raise ValueError("max_extensions must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_calls": self.api_calls,
            "seconds": self.seconds,
            "max_depth": self.max_depth,
            "max_nodes": self.max_nodes,
            "pursue_until_answered": self.pursue_until_answered,
            "max_extensions": self.max_extensions,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Budgets:
        defaults = cls()
        return cls(
            api_calls=int(raw.get("api_calls", defaults.api_calls)),
            seconds=float(raw.get("seconds", defaults.seconds)),
            max_depth=int(raw.get("max_depth", defaults.max_depth)),
            max_nodes=int(raw.get("max_nodes", defaults.max_nodes)),
            # Round-tripped so a RESUMED run keeps the pursuit setting the
            # operator chose. Read with the same defaults as a fresh Budgets,
            # so a row written before pursuit existed resumes with it ON rather
            # than with a silently different policy from the one in this file.
            pursue_until_answered=bool(
                raw.get("pursue_until_answered", defaults.pursue_until_answered)
            ),
            max_extensions=int(raw.get("max_extensions", defaults.max_extensions)),
        )


class BudgetTracker:
    """Mutable spend state for one run. Clock injectable for tests."""

    def __init__(self, budgets: Budgets, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.budgets = budgets
        # The limits as CONFIGURED. Each extension grants one more allowance of
        # this size, so eight extensions cost nine times the stated budget and not 256 times
        # — doubling reads as reasonable per step and is unaffordable by the
        # eighth, which is the shape of every quota bill nobody meant to run up.
        self._base = budgets
        self._clock = clock
        self._started = clock()
        self.api_calls_spent = 0
        self.nodes_created = 0
        self.txs_normalized = 0
        self.depth_horizon_nodes = 0
        self._prior_extensions: tuple[BudgetExtension, ...] = ()
        self._extensions: list[BudgetExtension] = []

    def seed_nodes(self, existing: int) -> None:
        self.nodes_created = existing

    def seed_spent(self, spent: dict[str, Any]) -> None:
        """Carry forward prior-run counters on resume. The wall clock is per
        run and intentionally not restored (see module docstring).

        Extensions carry forward too, and that is the whole ceiling: counted per
        run, eight resumes would buy sixty-four automatic extensions and the
        cap would bound nothing. A resume still gets everything it asked for —
        the operator names the new budget — it just does not get a fresh
        allowance of self-granted ones."""
        self.api_calls_spent = int(spent.get("api_calls", 0))
        self.txs_normalized = int(spent.get("txs_normalized", 0))
        recorded = spent.get("budget_extensions") or ()
        self._prior_extensions = tuple(
            BudgetExtension.from_dict(item) for item in recorded if isinstance(item, dict)
        )

    def charge_api(self, calls: int = 1) -> None:
        self.api_calls_spent += calls

    def charge_nodes(self, nodes: int = 1) -> None:
        self.nodes_created += nodes

    def charge_txs(self, txs: int) -> None:
        self.txs_normalized += txs

    def note_depth_horizon(self) -> None:
        self.depth_horizon_nodes += 1

    def elapsed(self) -> float:
        return self._clock() - self._started

    def exhausted(self) -> str | None:
        """Name of the first exhausted budget, or None."""
        if self.api_calls_spent >= self.budgets.api_calls:
            return "api_calls"
        if self.elapsed() >= self.budgets.seconds:
            return "seconds"
        if self.nodes_created >= self.budgets.max_nodes:
            return "max_nodes"
        return None

    # ── pursuit ──────────────────────────────────────────────────────────

    def extensions(self) -> tuple[BudgetExtension, ...]:
        """Every extension this INVESTIGATION has been granted, earlier runs
        included. What the report states and what the ceiling counts are the
        same list, so a report can never show more extensions than the number
        the tracker believed it had spent."""
        return self._prior_extensions + tuple(self._extensions)

    def may_extend(self) -> bool:
        """Is there any extension left to grant? Says nothing about whether one
        is WARRANTED — that is the engine's call, since only it knows which
        objectives are answered and whether the frontier still holds work."""
        return (
            self.budgets.pursue_until_answered
            and len(self.extensions()) < self.budgets.max_extensions
        )

    def extend(self, budget: str, unanswered: Sequence[str]) -> BudgetExtension:
        """Grant one more allowance of the CONFIGURED size on ``budget``.

        Raises rather than returning None on a budget that may not be extended,
        because both refusals are programming errors: ``max_depth`` is a
        semantic boundary (module docstring) and a caller that reached here past
        ``may_extend`` has lost the ceiling.
        """
        if budget not in EXTENDABLE_BUDGETS:
            raise ValueError(
                f"budget {budget!r} is not extendable — only {', '.join(EXTENDABLE_BUDGETS)} are "
                "costs; max_depth decides what the trace means and is the operator's to set"
            )
        if not self.may_extend():
            raise ValueError("no extension left to grant")
        previous = float(getattr(self.budgets, budget))
        granted = previous + float(getattr(self._base, budget))
        value: Any = granted if budget == "seconds" else int(granted)
        self.budgets = replace(self.budgets, **{budget: value})
        extension = BudgetExtension(
            budget=budget,
            previous=previous,
            granted=granted,
            unanswered=tuple(unanswered),
        )
        self._extensions.append(extension)
        return extension

    def spent_snapshot(self) -> dict[str, Any]:
        return {
            "api_calls": self.api_calls_spent,
            "seconds": round(self.elapsed(), 3),
            "nodes": self.nodes_created,
            "txs_normalized": self.txs_normalized,
            # Recorded with the spend and not with the budgets: `budgets` is
            # what the operator asked for and must keep saying so, or a resumed
            # run would inherit the raised numbers as if a human had chosen
            # them and the report would have nothing left to compare against.
            "budget_extensions": [e.to_dict() for e in self.extensions()],
        }
