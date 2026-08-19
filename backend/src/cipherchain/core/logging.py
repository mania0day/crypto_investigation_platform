"""Logging setup: structured lines carrying investigation context.

Every log line emitted while an investigation is bound carries its id, which
makes the application log a per-investigation audit trail — the foundation
for "every fetched transaction is attributable to a reason" (vision §2).
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar, Token

_investigation_id: ContextVar[str | None] = ContextVar("investigation_id", default=None)

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s [inv:%(investigation_id)s] %(message)s"
_CONFIGURED_FLAG = "_cipherchain_configured"


class InvestigationContextFilter(logging.Filter):
    """Injects the bound investigation id (or '-') into every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.investigation_id = _investigation_id.get() or "-"
        return True


def bind_investigation(investigation_id: str) -> Token[str | None]:
    """Bind an investigation id to the current async context."""
    return _investigation_id.set(investigation_id)


def unbind_investigation(token: Token[str | None]) -> None:
    _investigation_id.reset(token)


def current_investigation_id() -> str | None:
    return _investigation_id.get()


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotent root-logger configuration for the cipherchain process."""
    root = logging.getLogger()
    _silence_credential_loggers()
    if getattr(root, _CONFIGURED_FLAG, False):
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.addFilter(InvestigationContextFilter())
    root.addHandler(handler)
    root.setLevel(level)
    setattr(root, _CONFIGURED_FLAG, True)


def _silence_credential_loggers() -> None:
    """Keep secret-bearing request URLs out of the logs.

    httpx/httpcore log the full request URL at INFO, and provider
    credentials travel in those URLs (``?apikey=``, ``/v3/<key>``, …).
    Raising these loggers to WARNING stops every provider key from being
    written to the (retained, per-investigation) audit log. See
    docs/research/REVIEW_FINDINGS.md #12.
    """
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
