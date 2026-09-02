"""Runtime settings, loaded from the environment / repo-root ``.env``.

Every provider credential is optional: the pool routes around absent
providers and the system degrades gracefully to whatever is configured
(vision principle 9). Keys are deployment config, never architecture.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Resolved relative to CWD: repo root or backend/ both work.
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://cipherchain:cipherchain@localhost:5432/cipherchain"

    # API-key auth (cipherchain.api.auth). ON unless a deployment explicitly turns
    # it off, because the failure mode of the other default is silent: a host
    # that forgot to set this would ship an open investigation console, and
    # nothing would look wrong. Set AUTH_ENABLED=false only for a local run —
    # ApiKeyAuth logs a banner every time it starts up disabled.
    auth_enabled: bool = True

    # Class A / B provider credentials (docs/research/PROVIDER_INVENTORY.md)
    etherscan_api_key: str | None = None
    drpc_api_key: str | None = None
    ankr_api_key: str | None = None
    infura_api_key: str | None = None
    alchemy_api_key: str | None = None
    quicknode_endpoint_url: str | None = None
    chainstack_endpoint_url: str | None = None
    chainstack_platform_api_key: str | None = None
    getblock_access_token: str | None = None
    # Tron works without a key; a free key only raises the rate limit.
    trongrid_api_key: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (tests construct Settings directly)."""
    return Settings()
