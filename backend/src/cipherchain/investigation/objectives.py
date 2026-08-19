"""The two v1 objectives and their trace directions (frozen scope)."""

from __future__ import annotations

import enum

from cipherchain.core.models import Direction


class Objective(enum.StrEnum):
    FIND_PREV_VASP = "find_prev_vasp"
    FIND_NEXT_VASP = "find_next_vasp"

    @property
    def direction(self) -> Direction:
        return Direction.BACKWARD if self is Objective.FIND_PREV_VASP else Direction.FORWARD
