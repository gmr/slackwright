"""One test suite for every form store.

The Valkey and PostgreSQL tests need the services in ``compose.yaml``;
see ``tests/services.py``.

"""

import asyncio
import time
import typing
import unittest
import uuid

from slackwright import forms
from tests import services


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

    async def test_release(self) -> None:
        test = typing.cast(unittest.TestCase, self)
        key = uuid.uuid4().hex
        test.assertTrue(await self.store.claim(key, 60))
        await self.store.release(key)
        test.assertTrue(await self.store.claim(key, 60))
        await self.store.release(uuid.uuid4().hex)  # no claim: no error

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


@services.requires_valkey
class ValkeyFormStoreTestCase(StoreTests, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = await services.valkey_store(self)


@services.requires_postgres
class PostgresFormStoreTestCase(StoreTests, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = await services.postgres_store(self)
        await self.store.create_schema()  # a second call does nothing

    async def test_purge_expired(self) -> None:
        await self.store.put(record('old'), 1)
        await self.store.put(record('new'), 60)
        await self.store.claim('old', 1)
        await asyncio.sleep(1.2)
        self.assertEqual(await self.store.purge_expired(), 2)
        self.assertIsNotNone(await self.store.get('new'))
