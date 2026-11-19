"""Turn Slack threads and files into Anthropic messages.

:func:`thread` reads a whole thread and gives back alternating
``user`` and ``assistant`` turns that start with a user turn, as the
Anthropic Messages API requires. The lower-level functions
(:func:`replace_mentions`, :func:`file_blocks`, :func:`file_to_block`)
are public for bots that build the history themselves.

File downloads send the bot token. :func:`http_client` makes a client
that sends the token only to Slack file hosts, on every request and on
every redirect.

"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import typing
import urllib.parse

import httpx

if typing.TYPE_CHECKING:
    import collections.abc

LOGGER = logging.getLogger(__name__)

ContentBlock = dict[str, typing.Any]
Message = dict[str, typing.Any]

SLACK_FILE_HOSTS = frozenset({'files.slack.com'})
"""Hosts that may receive the bot token."""

MAX_FILE_BYTES = 10 * 1024 * 1024
"""Largest file to download and give to the model."""

DOWNLOAD_CONCURRENCY = 4
"""File downloads at the same time, for each message."""

NAME_CONCURRENCY = 8
"""``users.info`` calls at the same time (Tier 4: 100+ per minute)."""

IMAGE_MEDIA_TYPES = frozenset(
    {'image/gif', 'image/jpeg', 'image/png', 'image/webp'}
)
TEXT_LIKE_MIMETYPES = frozenset(
    {
        'application/javascript',
        'application/json',
        'application/x-yaml',
        'application/xml',
    }
)
BINARY_AMBIGUOUS_MIMETYPES = frozenset(
    {'application/force-download', 'application/octet-stream'}
)

_MENTION_RE = re.compile(r'<@([A-Z0-9]+)(?:\|[^>]+)?>')
_UNSAFE_URL_CHARS = re.compile(r'[\\\s\x00-\x1f\x7f]')


class DisallowedURLError(httpx.RequestError):
    """A token-bearing request went to a host that is not allowed."""


def text_block(text: str) -> ContentBlock:
    """Return an Anthropic text content block."""
    return {'type': 'text', 'text': text}


def is_slack_file_url(
    url: str, allowed_hosts: collections.abc.Iterable[str] = SLACK_FILE_HOSTS
) -> bool:
    """Return ``True`` only if it is safe to send the token to ``url``.

    The URL must use https on the default port, have no user info, and
    have a host that is exactly one of ``allowed_hosts``. urllib and
    httpx must agree on the host, so that a parser difference cannot
    send the request to a different host. Never raises.

    """
    if not url or _UNSAFE_URL_CHARS.search(url):
        return False
    hosts = {host.lower() for host in allowed_hosts}
    try:
        parts = urllib.parse.urlsplit(url)
        return bool(
            parts.scheme == 'https'
            and '@' not in parts.netloc
            and parts.port in (None, 443)
            and parts.hostname in hosts
            and httpx.URL(url).host == parts.hostname
        )
    except (ValueError, TypeError, httpx.InvalidURL):
        return False


def http_client(
    bot_token: str,
    *,
    allowed_hosts: collections.abc.Iterable[str] = SLACK_FILE_HOSTS,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30,
) -> httpx.AsyncClient:
    """Return an HTTP client that sends ``bot_token`` to Slack hosts only.

    The caller must close the client.

    """
    hosts = frozenset(allowed_hosts)

    async def refuse_other_hosts(request: httpx.Request) -> None:
        if not is_slack_file_url(str(request.url), hosts):
            raise DisallowedURLError(
                f'Refusing to send the Slack token to {request.url.host!r}',
                request=request,
            )

    return httpx.AsyncClient(
        headers={'Authorization': f'Bearer {bot_token}'},
        event_hooks={'request': [refuse_other_hosts]},
        follow_redirects=False,
        timeout=timeout,
        transport=transport,
    )


def file_to_block(mimetype: str, content: bytes) -> ContentBlock:
    """Convert file bytes to an Anthropic content block.

    PDF becomes a ``document`` block, supported images become ``image``
    blocks, and text becomes a ``text`` block. Other types become a
    ``text`` block that explains the error to the model.

    """
    mimetype = mimetype.split(';', 1)[0].strip().lower()
    if mimetype == 'application/pdf' or mimetype in IMAGE_MEDIA_TYPES:
        return {
            'type': 'document' if mimetype == 'application/pdf' else 'image',
            'source': {
                'type': 'base64',
                'media_type': mimetype,
                'data': base64.b64encode(content).decode(),
            },
        }
    if mimetype.startswith('text/') or mimetype in TEXT_LIKE_MIMETYPES:
        return text_block(content.decode('utf-8', errors='replace'))
    if mimetype in BINARY_AMBIGUOUS_MIMETYPES:
        try:
            return text_block(content.decode('utf-8'))
        except UnicodeDecodeError:
            return text_block(
                'ERROR: a binary file could not be decoded as text. '
                'Supported types are PDF, images, and text files.'
            )
    return text_block(
        f'ERROR: unsupported file type {mimetype}. Supported types are '
        'PDF, images, and text files.'
    )


async def download(
    client: httpx.AsyncClient, url: str, max_bytes: int = MAX_FILE_BYTES
) -> ContentBlock | None:
    """Download one file and return it as a content block.

    Returns ``None`` if the download fails. Returns an error text block
    if the file is larger than ``max_bytes``.

    """
    try:
        async with client.stream('GET', url) as response:
            if response.status_code != 200:
                LOGGER.warning(
                    'Unable to download Slack file %s: HTTP %s',
                    url,
                    response.status_code,
                )
                return None
            mimetype = response.headers.get(
                'content-type', 'application/octet-stream'
            )
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    return text_block(
                        f'ERROR: the file is larger than the maximum of '
                        f'{max_bytes // (1024 * 1024)} MB.'
                    )
    except httpx.HTTPError as error:
        LOGGER.warning('Failed to download Slack file %s: %s', url, error)
        return None
    return file_to_block(mimetype, bytes(data))


async def file_blocks(
    client: httpx.AsyncClient, files: list[dict[str, typing.Any]]
) -> list[ContentBlock]:
    """Download a message's files and return them as content blocks.

    Downloads run at the same time, at most :data:`DOWNLOAD_CONCURRENCY`
    at once, because each file can use up to :data:`MAX_FILE_BYTES` of
    memory. Files that fail to download are left out.

    """
    urls = [
        url
        for item in files
        if (url := item.get('url_private_download') or item.get('url_private'))
    ]
    blocks = await _gather(
        [download(client, url) for url in urls], DOWNLOAD_CONCURRENCY
    )
    return [block for block in blocks if block is not None]


async def display_name(
    slack: typing.Any, user_id: str, cache: dict[str, str]
) -> str:
    """Return the display name of a Slack user. Uses ``cache``."""
    if user_id in cache:
        return cache[user_id]
    name = user_id
    try:
        response = await slack.users_info(user=user_id)
        user = response.get('user') or {}
        profile = user.get('profile') or {}
        name = (
            profile.get('display_name')
            or user.get('real_name')
            or user.get('name')
            or user_id
        )
    except Exception:  # noqa: BLE001 - a missing name must not fail a turn
        LOGGER.warning('Failed to get the display name of %s', user_id)
    cache[user_id] = name
    return name


async def replace_mentions(
    slack: typing.Any, text: str, cache: dict[str, str]
) -> str:
    """Replace ``<@U123>`` mentions with ``@display name``."""
    names = {
        user_id: await display_name(slack, user_id, cache)
        for user_id in {m.group(1) for m in _MENTION_RE.finditer(text)}
    }
    return _MENTION_RE.sub(lambda m: f'@{names[m.group(1)]}', text)


async def replies(
    slack: typing.Any, channel_id: str, thread_ts: str
) -> list[dict[str, typing.Any]]:
    """Return all messages in a thread, oldest first. Reads all pages."""
    messages: list[dict[str, typing.Any]] = []
    async for page in await slack.conversations_replies(
        channel=channel_id, ts=thread_ts, limit=200
    ):
        messages.extend(page.get('messages') or [])
    return messages


async def thread(
    slack: typing.Any,
    files_client: httpx.AsyncClient,
    channel_id: str,
    thread_ts: str,
    bot_user_id: str,
    max_messages: int = 50,
) -> list[Message]:
    """Read a thread and return it as Anthropic messages.

    Args:
        slack: A ``slack_sdk`` ``AsyncWebClient``.
        files_client: The client from :func:`http_client`, used to
            download files.
        channel_id: The channel of the thread.
        thread_ts: The ``ts`` of the thread's first message.
        bot_user_id: The bot's user ID. Its messages become
            ``assistant`` turns.
        max_messages: Only the last ``max_messages`` are used.

    """
    messages = await replies(slack, channel_id, thread_ts)
    return await to_messages(
        slack, files_client, messages[-max_messages:], bot_user_id
    )


async def to_messages(
    slack: typing.Any,
    files_client: httpx.AsyncClient,
    messages: list[dict[str, typing.Any]],
    bot_user_id: str,
) -> list[Message]:
    """Convert Slack messages (oldest first) to Anthropic messages.

    Each user message starts with ``<@name> [ts=...]``. Turns with the
    same role that follow each other are merged. Files in the bot's
    own messages become a text note, because the API accepts image
    and document blocks only in user turns.

    """
    cache: dict[str, str] = {}
    user_ids = {
        message['user']
        for message in messages
        if message.get('user') and not message.get('subtype')
    } | {
        match.group(1)
        for message in messages
        for match in _MENTION_RE.finditer(message.get('text') or '')
    }
    await _gather(
        [display_name(slack, user_id, cache) for user_id in user_ids],
        NAME_CONCURRENCY,
    )
    turns: list[tuple[str, list[ContentBlock]]] = []
    for message in messages:
        author = message.get('user')
        if message.get('subtype') or not author:
            continue
        text = (
            await replace_mentions(slack, message.get('text') or '', cache)
        ).strip()
        files: list[dict[str, typing.Any]] = message.get('files') or []
        if author == bot_user_id:
            role = 'assistant'
            names = ', '.join(f.get('name') or 'file' for f in files)
            parts = [text, f'[Attached: {names}]' if files else '']
            joined = '\n\n'.join(p for p in parts if p)
            content = [text_block(joined)] if joined else []
        else:
            role = 'user'
            blocks = await file_blocks(files_client, files) if files else []
            if not text and not blocks:
                continue
            name = await display_name(slack, author, cache)
            header = f'<@{name}> [ts={message.get("ts", "")}] {text}'
            content = [text_block(header.rstrip()), *blocks]
        if not content:
            continue
        if turns and turns[-1][0] == role:
            turns[-1][1].extend(content)
        else:
            turns.append((role, content))
    while turns and turns[0][0] == 'assistant':
        turns.pop(0)
    return [_collapse(role, parts) for role, parts in turns]


async def _gather[T](
    awaitables: list[collections.abc.Awaitable[T]], limit: int
) -> list[T]:
    """Await all of ``awaitables``, at most ``limit`` at once, in order."""
    semaphore = asyncio.Semaphore(limit)

    async def run(awaitable: collections.abc.Awaitable[T]) -> T:
        async with semaphore:
            return await awaitable

    return await asyncio.gather(*(run(a) for a in awaitables))


def _collapse(role: str, parts: list[ContentBlock]) -> Message:
    """Return ``{role, content}``. Text-only content becomes a string."""
    if all(part['type'] == 'text' for part in parts):
        return {
            'role': role,
            'content': '\n\n'.join(part['text'] for part in parts),
        }
    return {'role': role, 'content': parts}
