"""Credentials for the write surface — resolvable forever, revocable once.

The properties here are the ones an incident review depends on: a key is
resolvable by its public half after revocation (so the investigations it
authorized stay attributable), the moment it stopped being valid never moves,
and no id is ever silently rebound to a different secret.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.storage.repositories import ApiKeyRepository

T0 = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


async def issue(repo: ApiKeyRepository, key_id: str = "cc_live_1", **overrides: object) -> int:
    fields: dict[str, object] = {
        "key_id": key_id,
        "key_hash": "sha256:" + "a" * 64,
        "scopes": "investigations:write intel:read",
        "label": "case-team-3",
    }
    fields.update(overrides)
    return await repo.create(**fields)  # type: ignore[arg-type]


class TestLookup:
    async def test_a_key_is_resolved_by_its_public_id(self, session: AsyncSession) -> None:
        repo = ApiKeyRepository(session)
        await issue(repo)
        key = await repo.by_key_id("cc_live_1")
        assert key is not None
        assert key.scopes == "investigations:write intel:read"
        assert key.label == "case-team-3"
        assert key.is_active

    async def test_an_unknown_key_id_reads_as_nothing(self, session: AsyncSession) -> None:
        repo = ApiKeyRepository(session)
        assert await repo.by_key_id("cc_live_nope") is None

    async def test_a_revoked_key_still_resolves_so_auth_can_tell_it_from_a_typo(
        self, session: AsyncSession
    ) -> None:
        """Both are refused, but only one is a security event worth alerting on.
        Filtering revoked rows out of the lookup would erase that distinction
        before the auth layer ever sees it."""
        repo = ApiKeyRepository(session)
        await issue(repo)
        await repo.revoke("cc_live_1", at=T0)
        key = await repo.by_key_id("cc_live_1")
        assert key is not None
        assert not key.is_active
        assert key.revoked_at == T0


class TestRevocation:
    async def test_revoking_reports_whether_this_call_did_it(self, session: AsyncSession) -> None:
        repo = ApiKeyRepository(session)
        await issue(repo)
        assert await repo.revoke("cc_live_1", at=T0) is True
        assert await repo.revoke("cc_live_1", at=T1) is False

    async def test_revoking_twice_does_not_move_the_moment_it_stopped_being_valid(
        self, session: AsyncSession
    ) -> None:
        """An incident review reads `revoked_at` to decide which requests were
        made with a live credential. A later call overwriting it would move that
        boundary and quietly exonerate the wrong requests."""
        repo = ApiKeyRepository(session)
        await issue(repo)
        await repo.revoke("cc_live_1", at=T0)
        await repo.revoke("cc_live_1", at=T1)
        key = await repo.by_key_id("cc_live_1")
        assert key is not None and key.revoked_at == T0

    async def test_revoking_an_unknown_key_is_not_an_error_and_changes_nothing(
        self, session: AsyncSession
    ) -> None:
        repo = ApiKeyRepository(session)
        assert await repo.revoke("cc_live_nope") is False


class TestIdentity:
    async def test_a_key_id_is_never_rebound_to_a_different_secret(
        self, session: AsyncSession
    ) -> None:
        """Silently replacing the hash would revoke the old key with no
        revocation recorded, and every log line already written against that id
        would then name the wrong holder."""
        repo = ApiKeyRepository(session)
        await issue(repo)
        with pytest.raises(IntegrityError):
            await issue(repo, key_hash="sha256:" + "b" * 64)
