"""Valkey and PostgreSQL form stores.

Install the backend with ``slackwright[valkey]`` or
``slackwright[postgres]``. Each store uses a client or pool that the bot
already has, and never opens or closes connections itself.

"""

from __future__ import annotations

import typing

from slackwright import forms


class ValkeyFormStore:
    """A :class:`~slackwright.forms.FormStore` in Valkey (or Redis).

    Args:
        client: A ``valkey.asyncio.Valkey`` (or ``redis.asyncio.Redis``)
            client.
        prefix: The prefix of every key.

    Records and claims expire on their own after their TTL.

    """

    def __init__(
        self, client: typing.Any, *, prefix: str = 'slackwright:form:'
    ) -> None:
        self._client = client
        self._prefix = prefix

    async def put(self, record: forms.FormRecord, ttl: int) -> None:
        await self._client.set(
            f'{self._prefix}{record.form_id}',
            record.model_dump_json(),
            ex=ttl,
        )

    async def get(self, form_id: str) -> forms.FormRecord | None:
        data = await self._client.get(f'{self._prefix}{form_id}')
        if data is None:
            return None
        return forms.FormRecord.model_validate_json(data)

    async def claim(self, key: str, ttl: int) -> bool:
        # Form IDs have no ':', so claim keys cannot clash with them.
        return bool(
            await self._client.set(
                f'{self._prefix}claim:{key}', '1', nx=True, ex=ttl
            )
        )


class PostgresFormStore:
    """A :class:`~slackwright.forms.FormStore` in PostgreSQL.

    Args:
        pool: A ``psycopg_pool.AsyncConnectionPool``.
        forms_table: The table for form records.
        claims_table: The table for claims.

    Call :meth:`create_schema` once, and :meth:`purge_expired` on a
    schedule to delete old rows. Expired rows are never returned.

    """

    def __init__(
        self,
        pool: typing.Any,
        *,
        forms_table: str = 'slackwright_forms',
        claims_table: str = 'slackwright_form_claims',
    ) -> None:
        from psycopg import sql  # optional dependency

        self._pool = pool
        self._sql = sql
        self._forms = sql.Identifier(forms_table)
        self._claims = sql.Identifier(claims_table)

    async def create_schema(self) -> None:
        """Create the two tables if they do not exist."""
        sql = self._sql
        async with self._pool.connection() as conn:
            await conn.execute(
                sql.SQL(
                    'CREATE TABLE IF NOT EXISTS {} ('
                    'form_id TEXT PRIMARY KEY, '
                    'record JSONB NOT NULL, '
                    'expires_at TIMESTAMPTZ NOT NULL)'
                ).format(self._forms)
            )
            await conn.execute(
                sql.SQL(
                    'CREATE TABLE IF NOT EXISTS {} ('
                    'claim_key TEXT PRIMARY KEY, '
                    'expires_at TIMESTAMPTZ NOT NULL)'
                ).format(self._claims)
            )

    async def put(self, record: forms.FormRecord, ttl: int) -> None:
        query = self._sql.SQL(
            'INSERT INTO {} (form_id, record, expires_at) '
            'VALUES (%s, %s::jsonb, now() + make_interval(secs => %s)) '
            'ON CONFLICT (form_id) DO UPDATE '
            'SET record = EXCLUDED.record, expires_at = EXCLUDED.expires_at'
        ).format(self._forms)
        async with self._pool.connection() as conn:
            await conn.execute(
                query, (record.form_id, record.model_dump_json(), ttl)
            )

    async def get(self, form_id: str) -> forms.FormRecord | None:
        query = self._sql.SQL(
            'SELECT record FROM {} WHERE form_id = %s AND expires_at > now()'
        ).format(self._forms)
        async with self._pool.connection() as conn:
            cursor = await conn.execute(query, (form_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        return forms.FormRecord.model_validate(row[0])

    async def claim(self, key: str, ttl: int) -> bool:
        # An expired claim is replaced; a live claim is kept.
        query = self._sql.SQL(
            'INSERT INTO {0} (claim_key, expires_at) '
            'VALUES (%s, now() + make_interval(secs => %s)) '
            'ON CONFLICT (claim_key) DO UPDATE '
            'SET expires_at = EXCLUDED.expires_at '
            'WHERE {0}.expires_at <= now()'
        ).format(self._claims)
        async with self._pool.connection() as conn:
            cursor = await conn.execute(query, (key, ttl))
            return bool(cursor.rowcount == 1)

    async def purge_expired(self) -> int:
        """Delete expired records and claims. Return the rows deleted."""
        deleted = 0
        async with self._pool.connection() as conn:
            for table in (self._forms, self._claims):
                cursor = await conn.execute(
                    self._sql.SQL(
                        'DELETE FROM {} WHERE expires_at <= now()'
                    ).format(table)
                )
                deleted += cursor.rowcount
        return deleted
