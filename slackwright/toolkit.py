"""Expose slackwright to an LLM agent as tools.

A bot makes one :class:`Toolkit` for each agent turn. The toolkit is
bound to one channel and thread, so the agent cannot post anywhere
else unless the bot gives a ``can_post`` check.

``toolkit.anthropic()`` returns the tool definitions for the Anthropic
Messages API. ``await toolkit.call(name, arguments)`` runs a tool and
never raises for bad input or a Slack error: the result text tells the
agent what went wrong, so that it can try again.

"""

from __future__ import annotations

import collections.abc
import dataclasses
import json
import logging
import typing

import pydantic
from slack_sdk import errors

from slackwright import blocks as block_specs
from slackwright import render

LOGGER = logging.getLogger(__name__)

CanPost = collections.abc.Callable[[str, str], collections.abc.Awaitable[bool]]
"""``async can_post(channel_id, user_id) -> bool``."""


@dataclasses.dataclass(frozen=True)
class ToolResult:
    """The result of a tool call.

    ``text`` is JSON for the ``tool_result`` content. ``is_error`` maps
    to the ``is_error`` flag of an Anthropic ``tool_result``.

    """

    text: str
    is_error: bool = False

    @classmethod
    def ok(cls, **values: typing.Any) -> ToolResult:
        return cls(json.dumps({'ok': True, **values}))

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(json.dumps({'ok': False, 'error': message}), True)


class _Input(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra='forbid')

    channel_id: str | None = pydantic.Field(
        default=None,
        description=(
            'Optional channel ID to post to. Leave it out to post in '
            'the current thread.'
        ),
    )


class PostMarkdownInput(_Input):
    text: str = pydantic.Field(
        min_length=1,
        description=(
            'Standard Markdown: headings, bold, italic, links, lists, '
            'code blocks, tables, quotes. Long text is split into more '
            'than one message.'
        ),
    )


class PostBlocksInput(_Input):
    blocks: list[block_specs.BlockSpec] = pydantic.Field(
        min_length=1,
        max_length=block_specs.MESSAGE_BLOCK_LIMIT,
        description='The blocks of the message, top to bottom.',
    )
    fallback_text: str = pydantic.Field(
        min_length=1,
        max_length=3000,
        description='Short plain text for notifications.',
    )


@dataclasses.dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    model: type[_Input]
    handler: collections.abc.Callable[
        [Toolkit, typing.Any, str], collections.abc.Awaitable[ToolResult]
    ]


class Toolkit:
    """Slack tools bound to one channel, thread, and user.

    Args:
        slack: A ``slack_sdk`` ``AsyncWebClient``.
        channel_id: The channel the agent works in.
        thread_ts: The thread the agent replies in.
        user_id: The user the agent acts for.
        can_post: Optional ``async can_post(channel_id, user_id)``.
            When it is set, the tools accept a ``channel_id`` and post
            there if the check returns ``True``.
        max_posts: The maximum number of posts for this toolkit.
        allow_broadcasts: When ``False`` (the default), the tools
            change ``<!channel>``, ``<!here>``, ``<!everyone>``, and
            user group mentions in agent text to plain text. See
            :func:`slackwright.render.neutralize_broadcasts`.

    """

    def __init__(
        self,
        slack: typing.Any,
        *,
        channel_id: str,
        thread_ts: str,
        user_id: str,
        can_post: CanPost | None = None,
        max_posts: int = 5,
        allow_broadcasts: bool = False,
    ) -> None:
        self._slack = slack
        self._channel_id = channel_id
        self._thread_ts = thread_ts
        self._user_id = user_id
        self._can_post = can_post
        self._max_posts = max_posts
        self._allow_broadcasts = allow_broadcasts
        self._posts = 0

    def anthropic(self) -> list[dict[str, typing.Any]]:
        """Return the tool definitions for the Anthropic Messages API."""
        return [
            {
                'name': tool.name,
                'description': tool.description,
                'input_schema': self._schema(tool.model),
            }
            for tool in _TOOLS.values()
        ]

    async def call(
        self, name: str, arguments: dict[str, typing.Any]
    ) -> ToolResult:
        """Run the tool ``name`` with ``arguments``."""
        tool = _TOOLS.get(name)
        if tool is None:
            return ToolResult.error(f'unknown tool {name!r}')
        try:
            values = tool.model.model_validate(arguments)
        except pydantic.ValidationError as error:
            return ToolResult.error(_describe(error))
        channel_id = await self._target(values.channel_id)
        if channel_id is None:
            return ToolResult.error(
                f'you cannot post to channel {values.channel_id}'
            )
        if self._posts >= self._max_posts:
            return ToolResult.error(
                f'post limit reached ({self._max_posts} per turn)'
            )
        self._posts += 1
        try:
            return await tool.handler(self, values, channel_id)
        except ValueError as error:
            return ToolResult.error(str(error))
        except errors.SlackApiError as error:
            LOGGER.warning('Tool %s failed: %s', name, error)
            return ToolResult.error(
                f'Slack API error: {error.response.get("error")}'
            )

    async def _target(self, channel_id: str | None) -> str | None:
        """Return the channel to post to, or ``None`` if not allowed."""
        if channel_id is None or channel_id == self._channel_id:
            return self._channel_id
        if self._can_post is not None and await self._can_post(
            channel_id, self._user_id
        ):
            return channel_id
        return None

    def _thread_for(self, channel_id: str) -> str | None:
        return self._thread_ts if channel_id == self._channel_id else None

    def _schema(self, model: type[_Input]) -> dict[str, typing.Any]:
        schema = model.model_json_schema()
        schema.pop('title', None)
        if self._can_post is None:
            schema['properties'].pop('channel_id', None)
        return schema

    def _safe(self, text: str) -> str:
        """Return agent ``text`` with broadcasts neutralized if needed."""
        if self._allow_broadcasts:
            return text
        return render.neutralize_broadcasts(text)

    def _safe_block(
        self, spec: block_specs.BlockSpec
    ) -> block_specs.BlockSpec:
        if isinstance(spec, block_specs.MarkdownSpec):
            return spec.model_copy(update={'text': self._safe(spec.text)})
        if isinstance(spec, block_specs.ContextSpec):
            return spec.model_copy(
                update={'elements': [self._safe(e) for e in spec.elements]}
            )
        return spec

    async def _post_markdown(
        self, values: PostMarkdownInput, channel_id: str
    ) -> ToolResult:
        timestamps = await render.post_markdown(
            self._slack,
            channel_id,
            self._thread_for(channel_id),
            self._safe(values.text),
        )
        return ToolResult.ok(ts=timestamps)

    async def _post_blocks(
        self, values: PostBlocksInput, channel_id: str
    ) -> ToolResult:
        response = await self._slack.chat_postMessage(
            channel=channel_id,
            thread_ts=self._thread_for(channel_id),
            blocks=block_specs.to_blocks(
                [self._safe_block(spec) for spec in values.blocks]
            ),
            text=self._safe(values.fallback_text),
        )
        return ToolResult.ok(ts=response['ts'])


def _describe(error: pydantic.ValidationError) -> str:
    """Return validation errors as short text for the agent."""
    return '; '.join(
        f'{".".join(str(p) for p in item["loc"]) or "input"}: {item["msg"]}'
        for item in error.errors(include_url=False)
    )


_POST_MARKDOWN = _Tool(
    name='slack_post_markdown',
    description=(
        'Post a Markdown message in the current Slack thread. Use it only '
        'for content that is separate from your final answer, because '
        'your final answer is posted for you. Slack renders standard '
        'Markdown, including tables and code blocks. Example: '
        '{"text": "## Results\\n\\n| Name | Count |\\n|---|---|\\n| a | 1 |"}'
    ),
    model=PostMarkdownInput,
    handler=Toolkit._post_markdown,
)

_POST_BLOCKS = _Tool(
    name='slack_post_blocks',
    description=(
        'Post a message built from Slack blocks in the current thread. Use '
        'it for structured layouts: a header, Markdown sections, dividers, '
        'context notes, images, and tables. Max '
        f'{block_specs.MESSAGE_BLOCK_LIMIT} blocks, '
        f'{block_specs.MARKDOWN_TEXT_LIMIT:,} Markdown characters, and '
        f'{block_specs.TABLE_TEXT_LIMIT:,} table characters per message. '
        'Example: {"fallback_text": "Deploy status", "blocks": [{"type": '
        '"header", "text": "Deploy status"}, {"type": "table", "rows": '
        '[["Service", "State"], ["api", "ok"]]}]}'
    ),
    model=PostBlocksInput,
    handler=Toolkit._post_blocks,
)

_TOOLS = {tool.name: tool for tool in (_POST_MARKDOWN, _POST_BLOCKS)}
