"""Post LLM Markdown answers to Slack.

Slack renders standard Markdown natively through the ``markdown_text``
argument of ``chat.postMessage`` and the streaming methods: headers,
bold, links, lists, code with syntax highlight, tables, quotes, and
task lists. So this module does not convert Markdown. It only handles
the 12,000-character limit.

"""

from __future__ import annotations

import html
import logging
import re
import time
import typing

import markdown_it
from slack_sdk import errors

from slackwright import blocks

LOGGER = logging.getLogger(__name__)

MARKDOWN_LIMIT = blocks.MARKDOWN_TEXT_LIMIT
"""Characters of Markdown per message, and per stream."""

STREAM_MAX_SECONDS = 540
"""Stop a stream before Slack's 10-minute limit ends it."""

STREAM_MAX_TASKS = 50
"""Unique tasks per streamed message."""

TaskStatus = typing.Literal['pending', 'in_progress', 'complete', 'error']

_NOT_STREAMING = 'message_not_in_streaming_state'

_PARSER = markdown_it.MarkdownIt('commonmark').enable('table')

_ZWSP = '\u200b'
# A ``<!`` that does not start a strict Slack date token. The date
# pattern excludes ``<`` and ``>``, so no token can hide inside it.
# The format and the optional link share one field that can contain
# ``^``. Two fields that both match ``^`` make the time quadratic.
_SPECIAL = re.compile(r'<!(?!date\^\d+\^[^<>|]*\|[^<>]*>)')
# Bare words are blocked in code and URLs too. A regex cannot find
# code and links exactly as Slack does, and a zero-width space there
# costs less than a broadcast that gets through.
_BARE_BROADCAST = re.compile(
    r'(?<![A-Za-z0-9])@(channel|here|everyone|group)(?![A-Za-z0-9])',
    re.IGNORECASE,
)


def neutralize_broadcasts(text: str) -> str:
    """Return ``text`` with no mention that notifies a channel or group.

    Breaks every ``<!`` except a strict Slack date token such as
    ``<!date^1700000000^{date}|Nov 14>``. A zero-width space after the
    ``<`` changes tokens such as ``<!channel>``, ``<!here>``, and
    ``<!subteam^ID|@label>`` into literal text. Also blocks the bare
    words ``@channel``, ``@here``, ``@everyone``, and ``@group`` in any
    letter case with a zero-width space after the ``@``, also in
    code and URLs. User mentions such as ``<@U123>`` and email
    addresses such as ``x@here.com`` do not change.

    The :class:`~slackwright.toolkit.Toolkit` calls this on agent text
    unless the bot sets ``allow_broadcasts``. :func:`post_markdown` does
    not call it, so a bot can call it on its final answer before it
    posts that answer.

    """
    text = _SPECIAL.sub(f'<{_ZWSP}!', text)
    return _BARE_BROADCAST.sub(lambda m: f'@{_ZWSP}{m.group(1)}', text)


_MRKDWN_RULES = (
    (re.compile(r'^#{1,6}[ \t]+(.+?)[ \t#]*$', re.MULTILINE), r'**\1**'),
    (re.compile(r'^([ \t]*)[-*+][ \t]+', re.MULTILINE), '\\1• '),
    (re.compile(r'(?<![*\w])\*(?!\*)([^*\n]+?)\*(?![*\w])'), r'_\1_'),
    (re.compile(r'\*\*(.+?)\*\*|__(.+?)__'), r'*\1\2*'),
    (re.compile(r'~~(.+?)~~'), r'~\1~'),
)
# Code and link destinations must not change. They go out as
# placeholders before the rules run, and come back after. A link
# destination can contain one level of balanced parentheses.
_PROTECTED = re.compile(
    r'(?P<code>```.*?```|`[^`\n]+`)'
    r'|\[(?P<label>[^\]\n]+)\]'
    r'\((?P<url>https?://(?:[^()\s|]|\([^()\s|]*\))+)\)',
    re.DOTALL,
)
_PLACEHOLDER = re.compile('\x00(\\d+)\x00')


def to_mrkdwn(markdown: str) -> str:
    """Convert simple Markdown to Slack mrkdwn, for places that need it.

    Use it only where Slack does not render Markdown, such as a modal
    ``section`` block. Handles headings, bullet lists, bold, italic,
    strikethrough, and links. ``&``, ``<``, and ``>`` are escaped first,
    so the text cannot make a mention or a special token.

    """
    saved: list[str] = []

    def protect(match: re.Match[str]) -> str:
        saved.append(match['code'] or match['url'])
        placeholder = f'\x00{len(saved) - 1}\x00'
        if match['code']:
            return placeholder
        return f'<{placeholder}|{match["label"]}>'

    text = html.escape(markdown.replace('\x00', ''), quote=False)
    text = _PROTECTED.sub(protect, text)
    for pattern, replacement in _MRKDWN_RULES:
        text = pattern.sub(replacement, text)
    return _PLACEHOLDER.sub(lambda m: saved[int(m.group(1))], text)


def split_markdown(text: str, limit: int = MARKDOWN_LIMIT) -> list[str]:
    """Split ``text`` into parts of at most ``limit`` characters.

    Splits only between top-level blocks, never inside a code block,
    a table, or a list. A single block that is longer than ``limit`` is
    returned as one part, so the caller must handle it.

    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    # Use the line breaks of markdown-it. splitlines() also breaks at
    # characters such as U+2028, so the token maps do not align.
    lines = re.split(r'\r\n?|\n', text)
    env: dict[str, typing.Any] = {}
    spans = [
        span
        for token in _PARSER.parse(text, env)
        if token.level == 0 and token.nesting >= 0 and (span := token.map)
    ]
    blocks = ['\n'.join(lines[start:end]).strip() for start, end in spans]
    # Link reference definitions make no tokens. Each part gets a copy
    # of the top-level definitions that its links use. A definition in
    # a quote or a list is already in its block.
    definitions = {
        label: '\n'.join(lines[start:end]).strip()
        for label, ref in env.get('references', {}).items()
        for start, end in [ref['map']]
        if not any(s <= start < e for s, e in spans)
    }
    parts: list[tuple[str, set[str]]] = []
    current, used = '', set[str]()
    for block in blocks:
        candidate = f'{current}\n\n{block}' if current else block
        candidate_used = used | _labels(block, definitions)
        # The part, a blank line, then one definition on each line.
        extra = sum(len(definitions[label]) + 1 for label in candidate_used)
        if len(candidate) + (extra + 1 if extra else 0) <= limit:
            current, used = candidate, candidate_used
            continue
        if current:
            parts.append((current, used))
        current, used = block, _labels(block, definitions)
    if current:
        parts.append((current, used))
    return [
        '\n\n'.join(
            [part, '\n'.join(d for k, d in definitions.items() if k in used)]
        )
        if used
        else part
        for part, used in parts
    ]


def _labels(block: str, definitions: dict[str, str]) -> set[str]:
    """Return the labels of the definitions that ``block`` uses.

    markdown-it keys definitions by label in upper case, with each run
    of white space changed to one space and no space at the ends.

    """
    normalized = (
        ' '.join(block.upper().split()).replace('[ ', '[').replace(' ]', ']')
    )
    return {label for label in definitions if f'[{label}]' in normalized}


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

    A stream is for one task. Do not call its methods from more than
    one task at the same time.

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
        self._pending: list[str] = []
        self._unsent = ''
        self._stopped = False
        self._finalized = False

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
            or self._overflow
            or self._chars + len(markdown) > MARKDOWN_LIMIT
            or await self._expired()
        ):
            self._overflow.append(markdown)
            return
        self._chars += len(markdown)
        # The SDK keeps short text in a buffer and returns None. Keep a
        # copy until a send, so a stop that fails does not lose it.
        if await (await self._get()).append(markdown_text=markdown) is None:
            self._unsent += markdown
        else:
            self._unsent = ''

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
        self._unsent = ''

    async def close(self) -> list[str]:
        """Stop the stream and post any overflow text.

        Returns the ``ts`` values of overflow messages. If a post fails,
        the parts that were not posted stay, and the next call tries
        them again. Do not call it again before the last call returns.

        """
        await self._stop()
        self._pending.extend(split_markdown(''.join(self._overflow)))
        self._overflow = []
        timestamps: list[str] = []
        while self._pending:
            timestamps += await post_markdown(
                self._slack,
                self._channel_id,
                self._thread_ts,
                self._pending[0],
            )
            self._pending.pop(0)
        return timestamps

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
        # ``_stopped`` blocks new appends. ``_finalized`` is set only
        # when the stop succeeds, so a failed stop can be tried again.
        if self._finalized:
            return
        self._stopped = True
        if self._stream is not None:
            try:
                await self._stream.stop(chunks=self._open_tasks() or None)
            except errors.SlackApiError as error:
                # Slack stops an idle stream itself. Then the stream is
                # already final, so do not try to stop it again. Post
                # the buffered text that Slack did not get as overflow.
                if error.response.get('error') != _NOT_STREAMING:
                    raise
                if self._unsent:
                    self._overflow.insert(0, self._unsent)
        self._unsent = ''
        self._finalized = True

    def _open_tasks(self) -> list[dict[str, typing.Any]]:
        return [
            _task_chunk(
                task_id, title, 'error', 'Stopped before this step finished.'
            )
            for task_id, (title, status) in self._tasks.items()
            if status in ('pending', 'in_progress')
        ]


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
