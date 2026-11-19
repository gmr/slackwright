import asyncio
import unittest

from slackwright import inflight


class InflightTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        inflight.reset()
        self.addCleanup(
            setattr, inflight, 'SHUTDOWN_TIMEOUT', inflight.SHUTDOWN_TIMEOUT
        )

    async def test_drain_waits_for_work(self) -> None:
        release = asyncio.Event()

        async def work() -> None:
            async with inflight.track():
                await release.wait()

        task = asyncio.create_task(work())
        await asyncio.sleep(0)
        self.assertEqual(inflight.active(), 1)
        asyncio.get_running_loop().call_later(0.01, release.set)
        self.assertTrue(await inflight.wait_for_drain())
        await task
        self.assertEqual(inflight.active(), 0)

    async def test_drain_checks_new_work(self) -> None:
        inflight.SHUTDOWN_TIMEOUT = 0.05
        first, second = inflight.track(), inflight.track()
        await first.__aenter__()
        waiter = asyncio.create_task(inflight.wait_for_drain())
        await asyncio.sleep(0)
        # The second unit starts after the drain event is set and
        # before the waiter wakes up.
        await first.__aexit__(None, None, None)
        await second.__aenter__()
        self.assertFalse(await waiter)
        await second.__aexit__(None, None, None)

    async def test_drain_timeout(self) -> None:
        inflight.SHUTDOWN_TIMEOUT = 0.01
        async with inflight.track():
            self.assertFalse(await inflight.wait_for_drain())

    async def test_drain_with_no_work(self) -> None:
        self.assertTrue(await inflight.wait_for_drain())
