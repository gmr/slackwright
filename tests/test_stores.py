"""One test suite for every form store.

The Valkey and PostgreSQL tests run when ``VALKEY_URL`` and
``POSTGRES_URL`` are set (CI sets them), and are skipped otherwise.

"""

import asyncio
import os
import time
import typing
import unittest
import uuid

from slackwright import forms, stores

VALKEY_URL = os.environ.get('VALKEY_URL')
POSTGRES_URL = os.environ.get('POSTGRES_URL')


def record(form_id: str) -> forms.FormRecord:
    return forms.FormRecord(
        form_id=form_id,
        spec=forms.FormSpec.model_validate(
            {
                'title': 'Lunch',
                'fields': [
                    {
                        'type': 'select',
                        'id': 'meal',
                        'label': 'Meal',
                        'options': ['Soup', 'Salad'],
                    },
                ],
            }
        ),
        channel_id='C1',
        thread_ts='1.0',
        requester='U1',
        created_at=time.time(),
    )


class StoreTests:
    """Tests that every :class:`~slackwright.forms.FormStore` passes."""

    store: typing.Any

    async def test_put_and_get(self) -> None:
        form_id = uuid.uuid4().hex
        original = record(form_id)
        await self.store.put(original, 60)
        stored = await self.store.get(form_id)
        typing.cast(unittest.TestCase, self).assertEqual(stored, original)

    async def test_put_replaces(self) -> None:
        form_id = uuid.uuid4().hex
        first = record(form_id)
        await self.store.put(first, 60)
        second = first.model_copy(update={'message_ts': '2.0'})
        await self.store.put(second, 60)
        stored = await self.store.get(form_id)
        typing.cast(unittest.TestCase, self).assertEqual(
            stored and stored.message_ts, '2.0'
        )

    async def test_missing(self) -> None:
        test = typing.cast(unittest.TestCase, self)
        test.assertIsNone(await self.store.get(uuid.uuid4().hex))

    async def test_claim_once(self) -> None:
        test = typing.cast(unittest.TestCase, self)
        key = uuid.uuid4().hex
        results = await asyncio.gather(
            *(self.store.claim(key, 60) for _ in range(5))
        )
        test.assertEqual(sorted(results), [False] * 4 + [True])
        test.assertTrue(await self.store.claim(f'{key}:U2', 60))

    async def test_expiry(self) -> None:
        test = typing.cast(unittest.TestCase, self)
        form_id, key = uuid.uuid4().hex, uuid.uuid4().hex
        await self.store.put(record(form_id), 1)
        test.assertTrue(await self.store.claim(key, 1))
        await asyncio.sleep(1.2)
        test.assertIsNone(await self.store.get(form_id))
        test.assertTrue(await self.store.claim(key, 1))


class MemoryFormStoreTestCase(StoreTests, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = forms.MemoryFormStore()


@unittest.skipUnless(VALKEY_URL, 'VALKEY_URL is not set')
class ValkeyFormStoreTestCase(StoreTests, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from valkey import asyncio as valkey

        self.client = valkey.Valkey.from_url(str(VALKEY_URL))
        self.store = stores.ValkeyFormStore(
            self.client, prefix=f'test:{uuid.uuid4().hex}:'
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()


@unittest.skipUnless(POSTGRES_URL, 'POSTGRES_URL is not set')
class PostgresFormStoreTestCase(StoreTests, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import psycopg_pool

        self.pool = psycopg_pool.AsyncConnectionPool(
            str(POSTGRES_URL), open=False
        )
        await self.pool.open()
        self.addAsyncCleanup(self.pool.close)
        suffix = uuid.uuid4().hex[:8]
        self.store = stores.PostgresFormStore(
            self.pool,
            forms_table=f'forms_{suffix}',
            claims_table=f'claims_{suffix}',
        )
        # Cleanups run in reverse order, so the drop runs before close.
        self.addAsyncCleanup(self._drop, f'forms_{suffix}', f'claims_{suffix}')
        await self.store.create_schema()
        await self.store.create_schema()  # a second call does nothing

    async def _drop(self, *tables: str) -> None:
        async with self.pool.connection() as conn:
            for table in tables:
                await conn.execute(f'DROP TABLE IF EXISTS {table}')

    async def test_purge_expired(self) -> None:
        await self.store.put(record('old'), 1)
        await self.store.put(record('new'), 60)
        await self.store.claim('old', 1)
        await asyncio.sleep(1.2)
        self.assertEqual(await self.store.purge_expired(), 2)
        self.assertIsNotNone(await self.store.get('new'))
