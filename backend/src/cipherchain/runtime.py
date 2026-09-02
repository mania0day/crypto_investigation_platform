"""Composition root: wire pool, clients, and adapters from Settings.

The ONLY place where vendors, chains, and configuration meet. Everything
here is wiring — adding an EVM chain or a vendor endpoint is an edit to
this module's tables, never to an adapter or the engine.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.analysis.assets import build_asset_policy
from cipherchain.analysis.attribution.loader import build_attributor
from cipherchain.analysis.heuristics import ALL_DETECTORS, detect_service_endpoint
from cipherchain.chains.base import ChainRegistry
from cipherchain.chains.bitcoin import BitcoinAdapter
from cipherchain.chains.bridges import BridgeRegistry, build_bridge_registry
from cipherchain.chains.evm import ETHEREUM_CONFIG, POLYGON_CONFIG, EvmAdapter
from cipherchain.chains.solana import SolanaAdapter
from cipherchain.chains.tron import TronAdapter
from cipherchain.core.config import Settings
from cipherchain.investigation.attribution import Attributor
from cipherchain.investigation.engine import InvestigationEngine
from cipherchain.providers.cache import CacheBackend, InMemoryCache
from cipherchain.providers.clients import (
    BlockscoutProvider,
    EtherscanV2Provider,
    EvmRpcProvider,
    ExplorerFetchProvider,
    MempoolSpaceProvider,
    SolanaRpcProvider,
    TronGridProvider,
)
from cipherchain.providers.pool import ProviderLimits, ProviderPool

# Additional EVM chains land here as config — no adapter code required.
_EVM_CONFIGS = (ETHEREUM_CONFIG, POLYGON_CONFIG)

# Per-chain RPC endpoint templates. {key} is substituted from settings.
_RPC_TEMPLATES: dict[str, list[tuple[str, str, str]]] = {
    # chain: [(vendor, settings-attr, url-template)]
    "ethereum": [
        ("drpc", "drpc_api_key", "https://lb.drpc.org/ogrpc?network=ethereum&dkey={key}"),
        ("alchemy", "alchemy_api_key", "https://eth-mainnet.g.alchemy.com/v2/{key}"),
        ("ankr", "ankr_api_key", "https://rpc.ankr.com/eth/{key}"),
        # ETH mainnet may be disabled on the Infura project; the breaker
        # takes it out of rotation if so (verified 2026-08-07).
        ("infura", "infura_api_key", "https://mainnet.infura.io/v3/{key}"),
    ],
    "polygon": [
        ("drpc", "drpc_api_key", "https://lb.drpc.org/ogrpc?network=polygon&dkey={key}"),
        ("alchemy", "alchemy_api_key", "https://polygon-mainnet.g.alchemy.com/v2/{key}"),
        ("ankr", "ankr_api_key", "https://rpc.ankr.com/polygon/{key}"),
    ],
    "solana": [
        ("alchemy", "alchemy_api_key", "https://solana-mainnet.g.alchemy.com/v2/{key}"),
        ("infura", "infura_api_key", "https://solana-mainnet.infura.io/v3/{key}"),
    ],
}


def _rpc_endpoints(settings: Settings, chain: str) -> list[tuple[str, str]]:
    """Configured RPC endpoints for a chain — only those whose key is set."""
    endpoints: list[tuple[str, str]] = []
    for vendor, attr, template in _RPC_TEMPLATES.get(chain, []):
        key = getattr(settings, attr, None)
        if key:
            endpoints.append((vendor, template.format(key=key)))
    # These are full URLs, not {key} templates — one endpoint is typically
    # provisioned per chain. Wire Ethereum only unless a dedicated URL exists.
    if chain == "ethereum":
        if settings.quicknode_endpoint_url:
            endpoints.append(("quicknode", settings.quicknode_endpoint_url))
        if settings.chainstack_endpoint_url:
            endpoints.append(("chainstack", settings.chainstack_endpoint_url))
        token = settings.getblock_access_token
        if token:
            url = (
                token
                if token.startswith(("http://", "https://"))
                else f"https://go.getblock.io/{token}/"
            )
            endpoints.append(("getblock", url))
    return endpoints


def build_provider_pool(
    settings: Settings, http: httpx.AsyncClient, *, cache: CacheBackend | None = None
) -> ProviderPool:
    pool = ProviderPool(cache=cache if cache is not None else InMemoryCache())
    # Bitcoin — keyless public instance: stay polite.
    pool.register(
        MempoolSpaceProvider(http), limits=ProviderLimits(rate_per_sec=2, burst=2), priority=10
    )
    if settings.etherscan_api_key:
        pool.register(
            EtherscanV2Provider(
                http,
                api_key=settings.etherscan_api_key,
                chain_ids={c.chain: c.etherscan_chain_id for c in _EVM_CONFIGS},
            ),
            limits=ProviderLimits(rate_per_sec=4, burst=4),
            priority=10,
        )
    for config in _EVM_CONFIGS:
        for position, (name, url) in enumerate(_rpc_endpoints(settings, config.chain)):
            pool.register(
                EvmRpcProvider(http, name=f"{name}/{config.chain}", url=url, chain=config.chain),
                limits=ProviderLimits(rate_per_sec=5, burst=5),
                priority=20 + position,
            )
    for position, (name, url) in enumerate(_rpc_endpoints(settings, "solana")):
        pool.register(
            SolanaRpcProvider(http, name=f"{name}/solana", url=url),
            limits=ProviderLimits(rate_per_sec=5, burst=5),
            priority=20 + position,
        )
    # Tron works keylessly; a free key only raises the rate limit.
    pool.register(
        TronGridProvider(http, api_key=settings.trongrid_api_key),
        limits=ProviderLimits(
            rate_per_sec=5 if settings.trongrid_api_key else 2,
            burst=5 if settings.trongrid_api_key else 2,
        ),
        priority=10,
    )
    # The fallback tier (REACHING_THE_VASP.md §5). Keyless, so it is always
    # registered, and deliberately LAST: priority 90 means the pool only
    # reaches it once every keyed provider for that chain has failed or spent
    # its quota. Rate-limited well below the others because it costs nothing —
    # a trace that runs out of allowance should slow down, not stop.
    pool.register(
        BlockscoutProvider(http),
        limits=ProviderLimits(rate_per_sec=1, burst=2),
        priority=90,
    )
    # The floor: public explorer PAGES, read at crawl speed (REACHING_THE_VASP.md
    # §5, tier 3 — "if api exhausted then use scraping idc if it takes time").
    # Priority 95 puts it below Blockscout's 90, so the ordering of the whole
    # pool is keyed providers, then the keyless API tier, then this. It is only
    # ever reached when everything above it has failed or spent its quota.
    #
    # Registered unconditionally because it needs no key. The pool limit here is
    # an outer bound only: the provider paces itself from what the site says it
    # allows (ExplorerFetchProvider.DEFAULT_RATE_PER_SEC, measured against 3xpl's
    # published `x-ratelimit-limit: 25`), and the slower of the two governs. That
    # is deliberate — the rate that matters is a property of the SITE, so it
    # belongs beside the site table and not in this wiring, where it would be
    # edited by someone tuning a pool without knowing what the number cost.
    #
    # Exceeding it is not a throttle: the site serves a bot-check, then blocks
    # the whole host for an hour. So this tier is somebody's public web server,
    # it obeys their robots.txt, and it reads slowly on purpose.
    # It serves Ethereum and Tron (DEFAULT_SITES) and declines every other chain
    # rather than guessing a URL layout, so registering it costs them nothing.
    #
    # Tron is why the registration matters most. Blockscout above is EVM-only,
    # so until this tier learned Tron's row dialect a Tron trace had exactly one
    # provider — TronGrid at priority 10 — and a spent or throttled TronGrid
    # ended that trace instead of slowing it: 1589 of 1592 addresses came back
    # with no label at all on the run that prompted the work. Tron carries more
    # VASP labels than any other chain here, so it was the worst one to have no
    # floor under it. It still pages ONLY on TronGrid's cursor, which this tier
    # cannot mint, so what it restores is the first page of an address and not
    # the whole history — see explorer_fetch's module docstring.
    pool.register(
        ExplorerFetchProvider(http),
        limits=ProviderLimits(rate_per_sec=0.5, burst=1),
        priority=95,
    )
    return pool


DEFAULT_BRIDGES_DIR = Path(__file__).resolve().parents[3] / "bridges"


def build_chain_registry(
    pool: ProviderPool, *, bridges: BridgeRegistry | None = None
) -> ChainRegistry:
    # Bridge contracts are operator-supplied data (see bridges/README.md);
    # absent a pack the registry is empty and no bridge findings are emitted.
    bridge_registry = bridges if bridges is not None else build_bridge_registry(DEFAULT_BRIDGES_DIR)
    registry = ChainRegistry()
    registry.register(BitcoinAdapter(pool))
    registry.register(SolanaAdapter(pool))
    registry.register(TronAdapter(pool))
    for config in _EVM_CONFIGS:
        registry.register(EvmAdapter(config, pool, bridges=bridge_registry))
    return registry


def build_engine(
    registry: ChainRegistry,
    session_factory: async_sessionmaker[AsyncSession],
    attributor: Attributor | None = None,
    *,
    labels_dir: Path | None = None,
    assets_dir: Path | None = None,
) -> InvestigationEngine:
    """Engine wiring, with the analysis layer attached.

    Attribution defaults to the label store (vendored OFAC sanctions plus
    any operator labelpacks). Detectors are Class F consumers: they read
    stored movements and never reach a provider.
    """
    return InvestigationEngine(
        registry,
        session_factory,
        attributor if attributor is not None else build_attributor(labels_dir),
        detectors=ALL_DETECTORS,
        service_detector=detect_service_endpoint,
        # Heuristics may only point at movements in an asset whose provenance
        # is established — native, or a token contract verified on-chain before
        # shipping. Without this an attacker-deployed token manufactures the
        # evidence (docs/research/DEPOSIT_ADDRESS_DESIGN.md §2.1).
        evidence_assets=build_asset_policy(assets_dir).accepts,
    )
