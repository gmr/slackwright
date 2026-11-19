"""Post LLM Markdown answers to Slack.

Slack renders standard Markdown natively through the ``markdown_text``
argument of ``chat.postMessage`` and the streaming methods: headers,
bold, links, lists, code with syntax highlight, tables, quotes, and
task lists. So this module does not convert Markdown. It only handles
the 12,000-character limit.

"""

from __future__ import annotations

import logging
import time
import typing

import markdown_it

from slackwright import blocks

LOGGER = logging.getLogger(__name__)

MARKDOWN_LIMIT = blocks.MARKDOWN_TEXT_LIMIT
"""Characters of Markdown per message, and per stream."""

STREAM_MAX_SECONDS = 540
"""Stop a stream before Slack's 10-minute limit ends it."""

STREAM_MAX_TASKS = 50
"""Unique tasks per streamed message."""

TaskStatus = typing.Literal['pending', 'in_progress', 'complete', 'error']

_PARSER = markdown_it.MarkdownIt('commonmark').enable('table')


def split_markdown(text: str, limit: int = MARKDOWN_LIMIT) -> list[str]:
    """Split ``text`` into parts of at most ``limit`` characters.

    Splits only between top-level blocks, never inside a code block,
    a table, or a list. A single block that is longer than ``limit`` is
    returned as one part, so the caller must handle it.

    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    lines = text.splitlines()
    # ponytail: link reference definitions are not tokens and are lost
    # when a long answer is split; LLM output rarely uses them.
    blocks = [
        '\n'.join(lines[token.map[0] : token.map[1]]).strip()
        for token in _PARSER.parse(text)
        if token.level == 0 and token.map and token.nesting >= 0
    ]
    parts: list[str] = []
    current = ''
    for block in blocks:
        candidate = f'{current}\n\n{block}' if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        current = block
    if current:
        parts.append(current)
    return parts


async def post_markdown(
    slack: typing.Any, channel_id: str, thread_ts: str | None, text: str
) -> list[str]:
    """Post Markdown to a channel or thread. Return the message ``ts`` list.

    Text over :data:`MARKDOWN_LIMIT` characters is split into more than
    one message. A single block over the limit (for example, a very long
    code block) is uploaded as a Markdown file.

    """
    timestamps: list[str] = []
    for part in split_markdown(text):
        if len(part) > MARKDOWN_LIMIT:
            await slack.files_upload_v2(
                channel=channel_id,
                thread_ts=thread_ts,
                content=part,
                filename='response.md',
                title='Response',
            )
            continue
        response = await slack.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, markdown_text=part
        )
        timestamps.append(response['ts'])
    return timestamps


class Stream:
    """Stream a reply, with task cards for the agent's steps.

    Text over the per-stream limit is not sent to the stream. It is
    posted with :func:`post_markdown` when the stream closes. When the
    stream is near Slack's time limit, it is stopped, open tasks are
    marked as errors, and the rest of the text is posted after it.

    Args:
        slack: A ``slack_sdk`` ``AsyncWebClient``.
        channel_id: The channel of the thread.
        thread_ts: The thread to reply in.
        user_id: The user who receives the stream. Required by Slack
            in channels.
        team_id: The team of ``user_id``. Required by Slack in channels.
        task_display_mode: ``timeline`` shows each task as a card in
            the text. ``plan`` groups the tasks in one plan block.

    """

    def __init__(
        self,
        slack: typing.Any,
        channel_id: str,
        thread_ts: str,
        *,
        user_id: str | None = None,
        team_id: str | None = None,
        task_display_mode: typing.Literal['timeline', 'plan'] = 'timeline',
        buffer_size: int = 256,
        max_seconds: float = STREAM_MAX_SECONDS,
    ) -> None:
        self._slack = slack
        self._channel_id = channel_id
        self._thread_ts = thread_ts
        self._options: dict[str, typing.Any] = {
            'recipient_user_id': user_id,
            'recipient_team_id': team_id,
            'task_display_mode': task_display_mode,
            'buffer_size': buffer_size,
        }
        self._max_seconds = max_seconds
        self._stream: typing.Any = None
        self._started = 0.0
        self._chars = 0
        self._tasks: dict[str, tuple[str, TaskStatus]] = {}
        self._overflow: list[str] = []
        self._stopped = False

    @property
    def ts(self) -> str | None:
        """The ``ts`` of the streamed message, after the first send."""
        return self._stream.ts if self._stream else None

    async def text(self, markdown: str) -> None:
        """Add Markdown text to the reply."""
        if not markdown:
            return
        if (
            self._stopped
            or self._chars + len(markdown) > MARKDOWN_LIMIT
            or await self._expired()
        ):
            self._overflow.append(markdown)
            return
        self._chars += len(markdown)
        await (await self._get()).append(markdown_text=markdown)

    async def task(
        self,
        task_id: str,
        title: str,
        status: TaskStatus,
        *,
        details: str | None = None,
        output: str | None = None,
        sources: list[tuple[str, str]] | None = None,
    ) -> None:
        """Add or update a task card.

        ``sources`` is a list of ``(text, url)`` pairs. Updates after
        the stream stopped, and new tasks over the limit of 50, are
        dropped.

        """
        if self._stopped or await self._expired():
            return
        if task_id not in self._tasks and len(self._tasks) >= (
            STREAM_MAX_TASKS
        ):
            LOGGER.debug('Dropping task %s: task limit reached', task_id)
            return
        self._tasks[task_id] = (title, status)
        chunk = _task_chunk(task_id, title, status, details, output)
        if sources:
            chunk['sources'] = [
                {'type': 'url', 'text': text, 'url': url}
                for text, url in sources
            ]
        await (await self._get()).append(chunks=[chunk])

    async def close(self) -> list[str]:
        """Stop the stream and post any overflow text.

        Returns the ``ts`` values of overflow messages.

        """
        await self._stop()
        overflow, self._overflow = ''.join(self._overflow), []
        if not overflow.strip():
            return []
        return await post_markdown(
            self._slack, self._channel_id, self._thread_ts, overflow
        )

    async def _get(self) -> typing.Any:
        if self._stream is None:
            self._stream = await self._slack.chat_stream(
                channel=self._channel_id,
                thread_ts=self._thread_ts,
                **self._options,
            )
            self._started = time.monotonic()
        return self._stream

    async def _expired(self) -> bool:
        if (
            self._stream is None
            or time.monotonic() - self._started < self._max_seconds
        ):
            return False
        await self._stop()
        return True

    async def _stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._stream is None:
            return
        open_tasks = [
            _task_chunk(
                task_id, title, 'error', 'Stopped before this step finished.'
            )
            for task_id, (title, status) in self._tasks.items()
            if status in ('pending', 'in_progress')
        ]
        await self._stream.stop(chunks=open_tasks or None)


def _task_chunk(
    task_id: str,
    title: str,
    status: TaskStatus,
    details: str | None = None,
    output: str | None = None,
) -> dict[str, typing.Any]:
    """Return a ``task_update`` chunk. Empty fields are left out."""
    chunk: dict[str, typing.Any] = {
        'type': 'task_update',
        'id': task_id,
        'title': title,
        'status': status,
    }
    if details:
        chunk['details'] = details
    if output:
        chunk['output'] = output
    return chunk
