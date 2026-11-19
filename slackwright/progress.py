"""Show an agent's tool calls while it works.

With a :class:`~slackwright.render.Stream`, each tool call is a task
card in the streamed reply. Without one, the reporter sets the
assistant thread status (``assistant.threads.setStatus``).

Progress is best effort: a Slack or network error is logged and never
stops the agent's turn.

"""

from __future__ import annotations

import logging
import typing

import aiohttp
from slack_sdk import errors

if typing.TYPE_CHECKING:
    import collections.abc

    from slackwright import render

LOGGER = logging.getLogger(__name__)

_ERRORS = (errors.SlackClientError, aiohttp.ClientError, TimeoutError)
"""Errors that a progress update ignores. Cancellation is not one."""


class Reporter:
    """Report the start and end of each tool call."""

    def __init__(
        self,
        slack: typing.Any,
        channel_id: str,
        thread_ts: str,
        *,
        stream: render.Stream | None = None,
    ) -> None:
        self._slack = slack
        self._channel_id = channel_id
        self._thread_ts = thread_ts
        self._stream = stream
        self._running: dict[str, str] = {}

    async def start(self, call_id: str, title: str) -> None:
        """Report that a tool call started."""
        self._running[call_id] = title
        if self._stream is not None:
            await _quietly(self._stream.task(call_id, title, 'in_progress'))
        else:
            await self._set_status(f'{title}…')

    async def finish(
        self, call_id: str, *, ok: bool = True, output: str | None = None
    ) -> None:
        """Report that a tool call ended."""
        title = self._running.pop(call_id, call_id)
        if self._stream is not None:
            await _quietly(
                self._stream.task(
                    call_id,
                    title,
                    'complete' if ok else 'error',
                    output=output,
                )
            )
        elif self._running:
            await self._set_status(f'{next(iter(self._running.values()))}…')
        else:
            await self._set_status('')

    async def _set_status(self, status: str) -> None:
        await _quietly(
            self._slack.assistant_threads_setStatus(
                channel_id=self._channel_id,
                thread_ts=self._thread_ts,
                status=status,
            )
        )


async def _quietly(update: collections.abc.Awaitable[typing.Any]) -> None:
    """Await a progress update. Log and ignore a Slack or network error."""
    try:
        await update
    except _ERRORS as error:
        LOGGER.debug('Could not update progress: %r', error)
