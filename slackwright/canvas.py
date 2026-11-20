"""Create, edit, and read Slack canvases.

Canvas content is standard Markdown. The bot needs the
``canvases:read``, ``canvases:write``, and ``files:read`` scopes.

"""

from __future__ import annotations

import typing

from slack_sdk import errors

Operation = typing.Literal[
    'insert_at_start',
    'insert_at_end',
    'insert_after',
    'insert_before',
    'replace',
    'delete',
    'rename',
]

SECTION_OPERATIONS = frozenset(
    {'insert_after', 'insert_before', 'replace', 'delete'}
)
"""Operations that need exactly one section. ``replace`` without a
section would replace the whole canvas, so slackwright never allows
that."""


def check(
    operation: Operation,
    markdown: str | None,
    section: str | None,
    *,
    section_name: str = 'section_id',
) -> None:
    """Raise :class:`ValueError` if a change misses a required value.

    ``section_name`` is the argument name to use in the message.

    """
    if operation in SECTION_OPERATIONS and not section:
        raise ValueError(f'{operation} needs {section_name}')
    if operation != 'delete' and not markdown:
        raise ValueError(f'{operation} needs markdown')


async def create(
    slack: typing.Any,
    channel_id: str,
    title: str,
    markdown: str,
    *,
    channel_canvas: bool = False,
) -> str:
    """Create a canvas in ``channel_id``. Return its ID.

    By default the canvas is a new canvas tab in the channel. With
    ``channel_canvas``, it is the channel's own canvas. A channel has
    only one, so this raises :class:`ValueError` if it has one.

    """
    content = {'type': 'markdown', 'markdown': markdown}
    try:
        if channel_canvas:
            response = await slack.conversations_canvases_create(
                channel_id=channel_id, document_content=content, title=title
            )
        else:
            response = await slack.canvases_create(
                title=title, document_content=content, channel_id=channel_id
            )
    except errors.SlackApiError as error:
        if error.response.get('error') == 'channel_canvas_already_exists':
            raise ValueError(
                'this channel already has a channel canvas; create a '
                'canvas tab or edit that canvas'
            ) from error
        raise
    return str(response['canvas_id'])


async def find_section(
    slack: typing.Any, canvas_id: str, contains: str
) -> str:
    """Return the ID of the one section that contains ``contains``.

    Raises :class:`ValueError` unless exactly one section matches.

    """
    response = await slack.canvases_sections_lookup(
        canvas_id=canvas_id, criteria={'contains_text': contains}
    )
    sections = response.get('sections') or []
    if len(sections) != 1:
        raise ValueError(
            f'{len(sections)} sections contain {contains!r}; it must '
            'match exactly one section'
        )
    return str(sections[0]['id'])


async def edit(
    slack: typing.Any,
    canvas_id: str,
    operation: Operation,
    markdown: str | None = None,
    *,
    section_id: str | None = None,
) -> None:
    """Make one change to a canvas.

    ``markdown`` is the new content, or the new title for ``rename``.
    Operations in :data:`SECTION_OPERATIONS` need ``section_id``; get
    it with :func:`find_section`.

    """
    check(operation, markdown, section_id)
    change: dict[str, typing.Any] = {'operation': operation}
    if section_id:
        change['section_id'] = section_id
    if markdown and operation != 'delete':
        key = 'title_content' if operation == 'rename' else 'document_content'
        change[key] = {'type': 'markdown', 'markdown': markdown}
    await slack.canvases_edit(canvas_id=canvas_id, changes=[change])


async def read(slack: typing.Any, canvas_id: str) -> str:
    """Return the content of a canvas as Markdown."""
    response = await slack.canvases_getContent(
        canvas_id=canvas_id, content_type='markdown'
    )
    return str(response.get('content') or '')


async def shared_with(slack: typing.Any, canvas_id: str) -> set[str]:
    """Return the IDs of the conversations that a canvas is shared in."""
    response = await slack.files_info(file=canvas_id)
    file = response.get('file') or {}
    shares = file.get('shares') or {}
    return {
        *(file.get('channels') or []),
        *(file.get('groups') or []),
        *(file.get('ims') or []),
        *(shares.get('public') or {}),
        *(shares.get('private') or {}),
    }
