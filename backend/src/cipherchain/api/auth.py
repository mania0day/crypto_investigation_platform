"""API-key authentication — what must exist before CipherChain leaves 127.0.0.1.

``scripts/demo.sh`` binds the loopback interface on purpose, and until this
module existed that bind *was* the access control: every route — start an
expensive trace, read case material naming victims and suspects — was open to
anything that could reach the port. This dependency is the thing that makes
any non-local exposure defensible. Nothing about the deployment posture
changed on its own: a host that exposes the API without mounting this is
publishing an investigation console.

The mechanism is deliberately small (LABEL_INTELLIGENCE.md §6): operator-minted
keys, a bearer header, scopes. No accounts, no sessions, no passwords, no
OAuth — a second kind of user does not exist yet, and each of those features
is new attack surface bought on speculation.

Four decisions worth the words:

**The secret is never stored.** A key is a public ``key_id`` (it appears in
logs and audit rows) plus a secret shown exactly once, at mint. Only a scrypt
digest of the secret is persisted, so a stolen database dump yields nothing
replayable against the API. A bare sha256 would have been faster and wrong: a
credential digest has to be slow enough that a dump is not one wordlist away
from a working key.

**The key_id travels inside the token.** Verification is then a single indexed
lookup plus one KDF call. Hashing the whole opaque token instead would force a
scan of every stored key and an scrypt call per row — 54 ms times the number
of issued keys, on every request, so authentication would get slower each time
the operator minted a key.

**No verification cache.** scrypt costs ~54 ms per request and the obvious
optimization is to remember verified secrets in-process. That optimization
keeps a revoked key alive until the entry expires, and revocation is the
emergency lever — the one control that has to be instant. If throughput ever
demands a cache, it needs an explicit invalidation path on revoke; it does not
get a quiet TTL and it does not get a weaker KDF.

**No rejection distinguishes an unknown key from a wrong secret from a revoked
one.** Distinct bodies (or distinct response times) turn the endpoint into a
key-id oracle: an attacker learns which ids exist and then only has to guess
one secret. Absent keys therefore still pay the KDF, against a decoy digest,
so the failures cost the same to produce as well as saying the same thing.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Final

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.core.config import Settings, get_settings
from cipherchain.storage.repositories import ApiKeyRepository

logger = logging.getLogger(__name__)


class Scope(enum.StrEnum):
    """What a key is allowed to do.

    Flat and non-hierarchical on purpose: READ does not imply INVESTIGATE and
    INVESTIGATE does not imply READ. Starting a trace is the expensive
    operation — provider quota, minutes of work, rows written — so the
    read-only key a dashboard or a reviewing analyst carries must not be able
    to start one. An implication would have made that separation depend on
    which scope a route happened to name first.

    A key that both starts and reads investigations therefore holds *both*
    scopes; the mint CLI says so.
    """

    READ = "read"
    INVESTIGATE = "investigate"


# Token shape: "<key_id>.<secret>". The ``cc_`` prefix exists so a leaked token
# is recognizable on sight and greppable by a secret scanner:
#   cc_[0-9a-f]{16}\.[A-Za-z0-9_-]{43}
KEY_ID_PREFIX: Final = "cc_"
KEY_ID_BYTES: Final = 8
SECRET_BYTES: Final = 32
TOKEN_SEPARATOR: Final = "."

# scrypt parameters. N=2**15 is NOT a free upgrade: 128*N*r is then 32 MiB,
# exactly the default OpenSSL envelope limit hashlib passes down, and the call
# dies with "memory limit exceeded" — verified on this box. Raising N means
# passing ``maxmem`` explicitly as well. Measured cost at these values: ~54 ms.
_SCRYPT_N: Final = 2**14
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_SCRYPT_DKLEN: Final = 32
_SALT_BYTES: Final = 16
_SCRYPT_LABEL: Final = "scrypt"

_UNAUTHORIZED_DETAIL: Final = "invalid or missing API key"

_BEARER: Final = HTTPBearer(
    scheme_name="CipherChain API key",
    description="`Authorization: Bearer <key-id>.<secret>`",
    # Every rejection in this module is shaped here, not by FastAPI's own
    # default body — the wording is load-bearing (it must not vary).
    auto_error=False,
)


# ──────────────────────────────── value objects ────────────────────────────────


@dataclass(frozen=True, slots=True)
class AuthenticatedKey:
    """The caller, once the presented secret has been verified.

    Carries no digest and no secret: this object is what routes receive, log
    and attribute work to, so it holds only what is safe to write down.
    """

    key_id: str
    scopes: frozenset[str]
    label: str | None = None

    def has(self, scope: Scope | str) -> bool:
        return str(scope) in self.scopes


@dataclass(frozen=True, slots=True)
class MintedKey:
    """A newly created key — the only moment the secret exists in memory.

    ``secret`` is excluded from ``repr`` deliberately: a credential must not
    surface in a traceback, a log line or a debugger frame dump. The only code
    that may print it is the mint CLI, which prints it once and says so.
    """

    key_id: str
    secret: str = field(repr=False)
    scopes: frozenset[str] = frozenset()
    label: str | None = None

    @property
    def token(self) -> str:
        """The bearer token the operator hands out."""
        return f"{self.key_id}{TOKEN_SEPARATOR}{self.secret}"


@dataclass(frozen=True, slots=True)
class StoredKey:
    """An issued key as an administrator sees it.

    No digest field, on purpose. The stored hash has exactly one reader —
    :func:`verify_key` — and a read model carrying it would leak credential
    material into every admin listing, every ``print`` and every log line that
    ever formats one.
    """

    key_id: str
    scopes: frozenset[str]
    label: str | None
    created_at: datetime
    revoked_at: datetime | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class RevokeOutcome(enum.StrEnum):
    """What revoking actually did — the CLI must not claim more than happened."""

    REVOKED = "revoked"
    ALREADY_REVOKED = "already_revoked"
    UNKNOWN = "unknown"


# ──────────────────────────────── secret handling ────────────────────────────────


def generate_key_id() -> str:
    return f"{KEY_ID_PREFIX}{secrets.token_hex(KEY_ID_BYTES)}"


def generate_secret() -> str:
    return secrets.token_urlsafe(SECRET_BYTES)


def hash_secret(secret: str) -> str:
    """Derive the stored digest record for ``secret``.

    The record is self-describing — ``scrypt$N$r$p$salt$digest`` — so the cost
    parameters can be raised later without a migration and without invalidating
    keys minted under the old ones: every row states how to verify itself.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = _derive(secret, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return "$".join(
        (
            _SCRYPT_LABEL,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64(salt),
            _b64(derived),
        )
    )


def verify_secret(secret: str, record: str) -> bool:
    """Constant-time check of ``secret`` against a stored digest record.

    A record this function cannot parse verifies **false**: an unreadable
    credential row is not a credential row we trust, and the alternative —
    falling through to some other comparison — is how a corrupt or truncated
    hash column turns into an accepted password.
    """
    parsed = _parse_record(record)
    if parsed is None:
        return False
    salt, expected, n, r, p = parsed
    derived = _derive(secret, salt, n, r, p)
    # compare_digest, never ==: an early-exit comparison of a derived key leaks
    # its prefix through response time.
    return hmac.compare_digest(derived, expected)


def _derive(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        secret.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_SCRYPT_DKLEN
    )


def _parse_record(record: str) -> tuple[bytes, bytes, int, int, int] | None:
    parts = record.split("$")
    if len(parts) != 6 or parts[0] != _SCRYPT_LABEL:
        logger.warning("unreadable api key digest record (algorithm %r)", parts[0][:16])
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, expected = _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, TypeError):
        logger.warning("malformed api key digest record")
        return None
    if not salt or not expected or n < 2 or r < 1 or p < 1:
        logger.warning("api key digest record has unusable parameters")
        return None
    return salt, expected, n, r, p


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@lru_cache(maxsize=1)
def _decoy_record() -> str:
    """A real digest of a secret nobody holds.

    Verifying against this when the key_id is unknown makes the failed lookup
    cost the same wall-clock time as a failed secret. Without it, "no such
    key" answers in a millisecond and "wrong secret" answers in 54 — which is
    a key-id enumeration oracle written in latency instead of in JSON.
    """
    return hash_secret(generate_secret())


def parse_scopes(raw: str | None) -> frozenset[str]:
    """Read a stored scope string.

    Stored space-separated (the OAuth 6749 §3.3 convention) but parsed
    tolerantly, because an operator editing the column by hand writes commas.
    Unknown names are kept rather than dropped: this parser does not decide
    what a scope means, it only reports what the row says.
    """
    if not raw:
        return frozenset()
    return frozenset(part for part in raw.replace(",", " ").split() if part)


def format_scopes(scopes: Iterable[Scope | str]) -> str:
    return " ".join(sorted({str(scope) for scope in scopes}))


# ──────────────────────────────── credential operations ────────────────────────────────
#
# There is exactly one implementation of the ``api_keys`` table, and it is
# ``ApiKeyRepository``. This module carried a second one — an ``ApiKeyStore``
# with its own INSERT/UPDATE/SELECT over the same rows — and two implementations
# of a credential store is a security bug with a delay fuse: the day one of them
# learns something the other does not, the drift IS the vulnerability, and it is
# invisible because both halves still pass their own tests.
#
# What lives here is the part that is not storage, and must not be: secret
# generation, the KDF, the decoy digest that equalizes a failed lookup, and the
# scope grammar. The repository never sees a secret; these functions never write
# SQL.


async def mint_key(
    session: AsyncSession, scopes: Iterable[Scope | str], *, label: str | None = None
) -> MintedKey:
    """Mint a key. The returned secret is the only copy that will ever exist.

    Commits itself, unlike the label importers, which leave the commit to their
    caller. That asymmetry is deliberate: this function's return value cannot be
    reproduced, so a forgotten commit hands the operator a working-*looking*
    token for a row that was rolled back — a credential that fails first at 3am
    on the request that mattered.
    """
    requested = frozenset(str(scope) for scope in scopes)
    if not requested:
        raise ValueError("a key with no scopes can do nothing — name at least one")
    # A scope name is stored inside a space-separated string and read back by
    # splitting on spaces and commas, so a name containing either does not
    # survive the round trip — it comes back as SEVERAL scopes. Minting
    # ``["read investigate"]`` (one string, from a config file, an argv splat,
    # a caller that joined a list) therefore issued a key holding both
    # privileges, and the caller had asked for neither of them by name.
    # Refused rather than sanitized: there is no honest reading of a scope
    # whose name cannot be stored.
    unstorable = sorted(
        s for s in requested if not s or any(ch.isspace() or ch == "," for ch in s)
    )
    if unstorable:
        raise ValueError(
            "a scope name may not be empty or contain a comma or whitespace — the store "
            f"would read it back as several scopes: {', '.join(repr(s) for s in unstorable)}"
        )
    key_id = generate_key_id()
    secret = generate_secret()
    await ApiKeyRepository(session).create(
        key_id=key_id,
        key_hash=hash_secret(secret),
        scopes=format_scopes(requested),
        label=label,
    )
    await session.commit()
    logger.info("minted API key %s with scopes [%s]", key_id, format_scopes(requested))
    return MintedKey(key_id=key_id, secret=secret, scopes=requested, label=label)


async def revoke_key(session: AsyncSession, key_id: str) -> RevokeOutcome:
    """Kill a key. Takes effect on the next request — there is no cache.

    Reports which of the three things happened, because an operator revoking a
    mistyped id must be told, not reassured: "revoked" and "there was no such
    key" look identical from the outside and only one of them means the
    credential is dead.
    """
    keys = ApiKeyRepository(session)
    if await keys.revoke(key_id):
        await session.commit()
        logger.warning("revoked API key %s", key_id)
        return RevokeOutcome.REVOKED
    # Nothing was written, so there is nothing to roll back — the second read
    # only separates "already revoked" from "never existed".
    return RevokeOutcome.ALREADY_REVOKED if await keys.by_key_id(key_id) else RevokeOutcome.UNKNOWN


async def list_keys(session: AsyncSession) -> list[StoredKey]:
    """Every issued key, active or not — an audit needs the dead ones too.

    The stored digest is dropped here rather than carried into
    :class:`StoredKey`: this listing is printed by the mint CLI and pasted into
    tickets, and a read model that held credential material would leak it into
    every one of those places.
    """
    return [
        StoredKey(
            key_id=row.key_id,
            scopes=parse_scopes(row.scopes),
            label=row.label,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
        )
        for row in await ApiKeyRepository(session).list_keys()
    ]


async def verify_key(session: AsyncSession, key_id: str, secret: str) -> AuthenticatedKey | None:
    """Resolve a presented (key_id, secret) pair, or ``None``.

    One return value for every failure — unknown id, wrong secret, revoked key —
    because a caller that *could* tell them apart would eventually report the
    difference to a client, and that is the enumeration oracle the module
    docstring refuses to build. An absent row still runs the KDF against a decoy
    digest so the failures also cost the same.

    Revocation is checked after verification rather than before for the same
    reason: both paths do identical work, and neither can be timed apart from
    the other.
    """
    record = await ApiKeyRepository(session).by_key_id(key_id)
    if record is None:
        verify_secret(secret, _decoy_record())
        return None
    if not verify_secret(secret, record.key_hash):
        return None
    if not record.is_active:
        logger.info("rejected revoked API key %s", key_id)
        return None
    return AuthenticatedKey(
        key_id=record.key_id, scopes=parse_scopes(record.scopes), label=record.label
    )


# ──────────────────────────────── the dependency ────────────────────────────────

# Who the caller is when auth is switched off. Named so that anything which
# attributes work to a key — an audit row, a log line — reads "auth-disabled"
# and says exactly what happened, instead of naming a key that was never shown.
AUTH_DISABLED_KEY: Final = AuthenticatedKey(
    key_id="auth-disabled",
    scopes=frozenset(str(scope) for scope in Scope),
    label="local development",
)


class ApiKeyAuth:
    """Builds the FastAPI dependencies that guard routes.

    Usage from the app factory (this module never mounts a route itself)::

        auth = ApiKeyAuth.from_settings(session_factory, settings)

        @app.get("/investigations/{id}", dependencies=[Depends(auth.requires(Scope.READ))])
        async def read(investigation_id: uuid.UUID) -> ...:
            ...

        # ...or, when the route needs to know WHICH key is calling. Bind the
        # Depends to a name first — a call in a default argument trips ruff's
        # B008, and the name means exactly the same thing to FastAPI:
        investigator = Depends(auth.requires(Scope.INVESTIGATE))

        @app.post("/investigations")
        async def start(key: AuthenticatedKey = investigator) -> ...:
            ...

    **Do not** write ``key: Annotated[AuthenticatedKey, Depends(auth.requires(…))]``
    inside the app factory. Every module here carries ``from __future__ import
    annotations``, so that annotation stays a *string* and is evaluated later
    against the module's globals — where the local ``auth`` does not exist.
    FastAPI never sees the ``Depends``, reads the parameter as a query
    parameter, and answers 422 (or 500 once it has a default) without ever
    running the check. Both forms above put the ``Depends`` in a runtime
    position where the closure is real. This cost 21 red tests to find, and it
    fails in a direction that looks like a client bug rather than a broken
    guard.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        enabled: bool = True,
    ) -> None:
        self._sessions = session_factory
        self._enabled = enabled
        self._dependencies: dict[
            tuple[str, ...], Callable[[HTTPAuthorizationCredentials | None], Awaitable[
                AuthenticatedKey
            ]]
        ] = {}
        if enabled:
            logger.info("API-key auth enabled — guarded routes require a bearer key")
        else:
            # Loud on purpose, and three lines of it. An operator who turned
            # this off for a local run and then deployed must trip over the
            # fact in the first screen of logs.
            logger.warning("=" * 72)
            logger.warning(
                "!! API-KEY AUTH IS DISABLED (auth_enabled=false) !! "
                "every guarded route is open to anything that can reach this port."
            )
            logger.warning(
                "Bind 127.0.0.1 only. Do NOT expose this process to a network: "
                "the API serves case material and starts expensive traces."
            )
            logger.warning("=" * 72)

    @classmethod
    def from_settings(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
    ) -> ApiKeyAuth:
        settings = settings or get_settings()
        return cls(session_factory, enabled=settings.auth_enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def requires(
        self, *scopes: Scope | str
    ) -> Callable[[HTTPAuthorizationCredentials | None], Awaitable[AuthenticatedKey]]:
        """A dependency demanding **all** of ``scopes``.

        The same scope set always returns the same callable. FastAPI keys its
        per-request dependency cache on the callable's identity, so a fresh
        closure per call would authenticate a request once per declaration —
        paying the 54 ms KDF each time.
        """
        required = frozenset(str(scope) for scope in scopes)
        cache_key = tuple(sorted(required))
        cached = self._dependencies.get(cache_key)
        if cached is not None:
            return cached

        async def dependency(
            credentials: Annotated[
                HTTPAuthorizationCredentials | None, Depends(_BEARER)
            ] = None,
        ) -> AuthenticatedKey:
            token = credentials.credentials if credentials is not None else None
            return await self.authenticate(token, required=required)

        self._dependencies[cache_key] = dependency
        return dependency

    async def authenticate(
        self, token: str | None, *, required: frozenset[str] = frozenset()
    ) -> AuthenticatedKey:
        """Verify a bearer token and its scopes, or raise the HTTP error.

        401 for "we do not know who you are" — missing header, malformed
        header, unknown key, wrong secret, revoked key, all with one body.
        403 only once identity is established: naming the scope a *verified*
        key lacks describes the endpoint, not the key, so it is not an oracle.
        """
        if not self._enabled:
            return AUTH_DISABLED_KEY
        if not token:
            raise _unauthorized()
        key_id, separator, secret = token.partition(TOKEN_SEPARATOR)
        if not separator or not key_id or not secret:
            # Never a valid token shape, so no lookup happened and there is
            # nothing to equalize the timing of.
            raise _unauthorized()
        async with self._sessions() as session:
            key = await verify_key(session, key_id, secret)
        if key is None:
            raise _unauthorized()
        missing = required - key.scopes
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"this key lacks the required scope: {', '.join(sorted(missing))}",
            )
        return key


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_UNAUTHORIZED_DETAIL,
        # Tells the client HOW to authenticate without saying anything about
        # whether the key it presented exists (RFC 7235 §4.1).
        headers={"WWW-Authenticate": "Bearer"},
    )
