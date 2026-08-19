#!/usr/bin/env bash
# One command to run the CipherChain demo: database up, migrations applied, server
# serving the UI at http://127.0.0.1:8000
#
#   ./scripts/demo.sh
#
# Bound to 127.0.0.1 on purpose. The API is authenticated (every route but
# /healthz needs a key), and this script mints one for its own UI — so the page
# served at "/" carries a working credential and anything that can reach the
# port can use it. That is fine on loopback and nowhere else.
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${PORT:-8000}"
PG_PORT="${PG_PORT:-54330}"
CONTAINER="${CONTAINER:-cipherchain-dev-pg}"
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://cipherchain:cipherchain@127.0.0.1:${PG_PORT}/cipherchain}"

if [ ! -x .venv/bin/python ]; then
  echo "error: .venv missing — run: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

echo "==> database"
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    docker start "$CONTAINER" >/dev/null
  else
    docker run -d --name "$CONTAINER" \
      -e POSTGRES_USER=cipherchain -e POSTGRES_PASSWORD=cipherchain -e POSTGRES_DB=cipherchain \
      -p "127.0.0.1:${PG_PORT}:5432" postgres:16-alpine >/dev/null
  fi
fi
for _ in $(seq 1 60); do
  docker exec "$CONTAINER" pg_isready -U cipherchain -d cipherchain >/dev/null 2>&1 && break
  sleep 1
done
echo "    ready on 127.0.0.1:${PG_PORT}"

echo "==> migrations"
.venv/bin/python -m alembic upgrade head >/dev/null
echo "    schema up to date"

echo "==> labels"
# Idempotent: unchanged rows write nothing. First run ingests every pack;
# later runs touch only what changed in labels/.
.venv/bin/python scripts/import_labelpacks.py | tail -6

echo "==> api key"
# Auth defaults to ON, and the bundled single-file UI has no field to paste a
# key into. So the demo mints one and hands it to the server, which embeds it in
# the page it serves — rather than starting with AUTH_ENABLED=false, because a
# demo that only works with the guard switched off demonstrates a system nobody
# is going to deploy. The key is a real, scoped, revocable credential; the line
# printed below kills it.
CIPHERCHAIN_DEMO_API_KEY="$(.venv/bin/python - <<'PY'
import asyncio

from cipherchain.api.auth import Scope, mint_key
from cipherchain.core.config import get_settings
from cipherchain.storage.db import create_engine, create_session_factory


async def main() -> None:
    engine = create_engine(get_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            minted = await mint_key(
                session, [Scope.READ, Scope.INVESTIGATE], label="scripts/demo.sh"
            )
            print(minted.token)
    finally:
        await engine.dispose()


asyncio.run(main())
PY
)"
export CIPHERCHAIN_DEMO_API_KEY
# The secret is printed because this is a local dev key on a local dev database
# and the page at "/" carries it anyway — hiding it here would only stop the
# operator using curl. A key minted for anything else is printed once by
# scripts/manage_api_keys.py and never again.
echo "    minted ${CIPHERCHAIN_DEMO_API_KEY%%.*} (read, investigate) for the demo UI"
echo "    curl:   -H 'Authorization: Bearer ${CIPHERCHAIN_DEMO_API_KEY}'"
echo "    revoke: .venv/bin/python scripts/manage_api_keys.py revoke ${CIPHERCHAIN_DEMO_API_KEY%%.*}"

echo "==> server"
echo "    UI:   http://127.0.0.1:${PORT}/"
echo "    docs: http://127.0.0.1:${PORT}/docs  (Authorize with the token above)"
echo
exec .venv/bin/uvicorn cipherchain.api.app:create_app_from_settings --factory \
  --host 127.0.0.1 --port "$PORT"
