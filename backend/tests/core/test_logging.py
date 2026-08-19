import io
import logging

from cipherchain.core.logging import (
    InvestigationContextFilter,
    bind_investigation,
    current_investigation_id,
    unbind_investigation,
)


def _capture_logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("[inv:%(investigation_id)s] %(message)s"))
    handler.addFilter(InvestigationContextFilter())
    logger = logging.getLogger("cipherchain.test.capture")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


def test_unbound_context_logs_dash() -> None:
    logger, stream = _capture_logger()
    logger.info("hello")
    assert "[inv:-] hello" in stream.getvalue()


def test_bound_investigation_id_appears_and_resets() -> None:
    logger, stream = _capture_logger()
    token = bind_investigation("inv-123")
    try:
        assert current_investigation_id() == "inv-123"
        logger.info("expanding frontier")
    finally:
        unbind_investigation(token)
    logger.info("after reset")
    output = stream.getvalue()
    assert "[inv:inv-123] expanding frontier" in output
    assert "[inv:-] after reset" in output
    assert current_investigation_id() is None
