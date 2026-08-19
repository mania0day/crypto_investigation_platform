"""FastAPI surface — the headless v1 interface (vision: engine + API + report).

Thin by design: it validates input, starts/inspects investigations, and
exposes findings and pool metrics. All logic lives below it; the API owns
no investigation behavior of its own.
"""

from cipherchain.api.app import create_app

__all__ = ["create_app"]
