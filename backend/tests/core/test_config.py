import pytest

from cipherchain.core.config import Settings


def test_provider_keys_are_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ETHERSCAN_API_KEY", "ALCHEMY_API_KEY", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.etherscan_api_key is None
    assert s.database_url.startswith("postgresql+asyncpg://")


def test_env_vars_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ETHERSCAN_API_KEY", "test-key")
    s = Settings(_env_file=None)
    assert s.etherscan_api_key == "test-key"
