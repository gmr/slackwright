"""Track in-flight work so that shutdown can wait for it.

Slack events and interactions are often handled as background tasks.
Without tracking, a rolling deploy can stop the process while a reply
is half sent. :func:`track` counts the work around each handler, and
:func:`wait_for_drain` lets shutdown wait (up to
:data:`SHUTDOWN_TIMEOUT` seconds) for the work to finish.

"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import typing

if typing.TYPE_CHECKING:
    import collections.abc

LOGGER = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT: float = 15
"""Maximum seconds to wait for in-flight work during shutdown."""

_active: int = 0
_drain: asyncio.Event | None = None


def _get_drain() -> asyncio.Event:
    """Return the drain event. Create it on first use."""
    global _drain
    if _drain is None:
        _drain = asyncio.Event()
        _drain.set()
    return _drain


def active() -> int:
    """Return the number of in-flight units of work."""
    return _active


def reset() -> None:
    """Reset the module state (for tests)."""
    global _active, _drain
    _active = 0
    _drain = None


@contextlib.asynccontextmanager
async def track() -> collections.abc.AsyncGenerator[None]:
    """Count one unit of in-flight work for the time of the context."""
    global _active
    drain = _get_drain()
    _active += 1
    drain.clear()
    try:
        yield
    finally:
        _active -= 1
        if _active == 0:
            drain.set()


async def wait_for_drain() -> bool:
    """Wait until all in-flight work is done, or SHUTDOWN_TIMEOUT ends.

    Returns ``True`` if all work finished, ``False`` on timeout.

    """
    if _active == 0:
        return True
    LOGGER.info(
        'Waiting for %i in-flight task(s) to finish (timeout %.0fs)',
        _active,
        SHUTDOWN_TIMEOUT,
    )
    # New work can start after the drain event is set and before this
    # task wakes up, so check the count again after each wakeup.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SHUTDOWN_TIMEOUT
    while _active > 0:
        try:
            await asyncio.wait_for(
                _get_drain().wait(), max(deadline - loop.time(), 0)
            )
        except TimeoutError:
            LOGGER.warning(
                'Shutdown timeout with %i task(s) still in flight', _active
            )
            return False
    return True
