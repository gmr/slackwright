"""Valkey and PostgreSQL for the integration tests.

Start them with ``docker compose up -d --wait`` (see ``compose.yaml``).
CI starts them as service containers and sets ``VALKEY_URL`` and
``POSTGRES_URL``.

When a URL is set in the environment, the tests that need it always
run, so a broken service fails the build. When it is not set, the
tests use the ``compose.yaml`` port and are skipped if nothing answers.

"""

import os
import socket
import unittest
import urllib.parse
import uuid

from slackwright import stores


def _url(name: str, default: str) -> tuple[str, bool]:
    """Return the URL for a service and whether its tests should run."""
    url = os.environ.get(name)
    if url:
        return url, True
    parts = urllib.parse.urlsplit(default)
    try:
        with socket.create_connection(
            (parts.hostname or 'localhost', parts.port or 0), timeout=0.5
        ):
            return default, True
    except OSError:
        return default, False


VALKEY_URL, _VALKEY_UP = _url('VALKEY_URL', 'valkey://localhost:56379/0')
POSTGRES_URL, _POSTGRES_UP = _url(
    'POSTGRES_URL', 'postgresql://postgres:postgres@localhost:55432/postgres'
)

_HINT = 'is not running; start it with: docker compose up -d --wait'
requires_valkey = unittest.skipUnless(_VALKEY_UP, f'Valkey {_HINT}')
requires_postgres = unittest.skipUnless(_POSTGRES_UP, f'PostgreSQL {_HINT}')


async def valkey_store(
    test: unittest.IsolatedAsyncioTestCase,
) -> stores.ValkeyFormStore:
    """Return a Valkey store with its own key prefix for ``test``."""
    from valkey import asyncio as valkey

    client = valkey.Valkey.from_url(VALKEY_URL)
    test.addAsyncCleanup(client.aclose)
    return stores.ValkeyFormStore(client, prefix=f'test:{uuid.uuid4().hex}:')


async def postgres_store(
    test: unittest.IsolatedAsyncioTestCase,
) -> stores.PostgresFormStore:
    """Return a PostgreSQL store with its own tables for ``test``.

    The tables are dropped when the test ends.

    """
    import psycopg_pool

    pool = psycopg_pool.AsyncConnectionPool(POSTGRES_URL, open=False)
    await pool.open()
    test.addAsyncCleanup(pool.close)
    suffix = uuid.uuid4().hex[:8]
    tables = (f'forms_{suffix}', f'claims_{suffix}')
    store = stores.PostgresFormStore(
        pool, forms_table=tables[0], claims_table=tables[1]
    )

    async def drop() -> None:
        async with pool.connection() as conn:
            for table in tables:
                await conn.execute(f'DROP TABLE IF EXISTS {table}')

    # Cleanups run in reverse order, so the drop runs before the close.
    test.addAsyncCleanup(drop)
    await store.create_schema()
    return store
