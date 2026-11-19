"""Typed specs for Slack blocks.

An agent writes a list of :data:`BlockSpec` values. slackwright checks
Slack's limits and writes the Block Kit JSON, so a bad spec fails with
a clear error before anything is posted.

"""

from __future__ import annotations

import typing

import pydantic

MESSAGE_BLOCK_LIMIT = 50
TABLE_ROW_LIMIT = 100
TABLE_CELL_LIMIT = 20
TABLE_TEXT_LIMIT = 10_000
MARKDOWN_TEXT_LIMIT = 12_000


class _Spec(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra='forbid')


class MarkdownSpec(_Spec):
    """Standard Markdown, rendered by Slack."""

    type: typing.Literal['markdown']
    text: str = pydantic.Field(
        min_length=1,
        max_length=MARKDOWN_TEXT_LIMIT,
        description=(
            'Standard Markdown: headings, bold, italic, links, lists, '
            'code blocks, tables, quotes.'
        ),
    )

    def to_block(self) -> dict[str, typing.Any]:
        return {'type': 'markdown', 'text': self.text}


class HeaderSpec(_Spec):
    """A large bold header line."""

    type: typing.Literal['header']
    text: str = pydantic.Field(
        min_length=1, max_length=150, description='Plain text, no Markdown.'
    )

    def to_block(self) -> dict[str, typing.Any]:
        return {
            'type': 'header',
            'text': {'type': 'plain_text', 'text': self.text, 'emoji': True},
        }


class DividerSpec(_Spec):
    """A horizontal line."""

    type: typing.Literal['divider']

    def to_block(self) -> dict[str, typing.Any]:
        return {'type': 'divider'}


class ContextSpec(_Spec):
    """Small grey text, for notes and metadata."""

    type: typing.Literal['context']
    elements: list[
        typing.Annotated[str, pydantic.Field(min_length=1, max_length=3000)]
    ] = pydantic.Field(
        min_length=1,
        max_length=10,
        description=(
            'One to ten short texts in Slack mrkdwn: *bold*, _italic_, '
            '<https://example.com|label>. Not standard Markdown.'
        ),
    )

    def to_block(self) -> dict[str, typing.Any]:
        return {
            'type': 'context',
            'elements': [
                {'type': 'mrkdwn', 'text': text} for text in self.elements
            ],
        }


class ImageSpec(_Spec):
    """An image from a public URL."""

    type: typing.Literal['image']
    image_url: pydantic.HttpUrl = pydantic.Field(
        description='A public https URL of a PNG, JPEG, or GIF image.'
    )
    alt_text: str = pydantic.Field(
        min_length=1,
        max_length=2000,
        description='A description of the image for screen readers.',
    )
    title: str | None = pydantic.Field(
        default=None, max_length=2000, description='Optional caption.'
    )

    def to_block(self) -> dict[str, typing.Any]:
        block: dict[str, typing.Any] = {
            'type': 'image',
            'image_url': str(self.image_url),
            'alt_text': self.alt_text,
        }
        if self.title:
            block['title'] = {'type': 'plain_text', 'text': self.title}
        return block


class TableSpec(_Spec):
    """A table. The first row is the header row."""

    type: typing.Literal['table']
    rows: list[list[str]] = pydantic.Field(
        min_length=1,
        max_length=TABLE_ROW_LIMIT,
        description=(
            'Rows of plain-text cells. The first row is the header. All '
            'rows have the same number of cells (max '
            f'{TABLE_CELL_LIMIT}). Max {TABLE_ROW_LIMIT} rows and '
            f'{TABLE_TEXT_LIMIT:,} characters in all cells.'
        ),
    )
    align: list[typing.Literal['left', 'center', 'right'] | None] | None = (
        pydantic.Field(
            default=None,
            description='Optional alignment for each column, left to right.',
        )
    )

    @pydantic.model_validator(mode='after')
    def _check_shape(self) -> TableSpec:
        width = len(self.rows[0])
        if not 1 <= width <= TABLE_CELL_LIMIT:
            raise ValueError(
                f'each row must have 1 to {TABLE_CELL_LIMIT} cells'
            )
        if any(len(row) != width for row in self.rows):
            raise ValueError('all rows must have the same number of cells')
        if self.align is not None and len(self.align) > width:
            raise ValueError('align has more items than the table columns')
        if self.text_length() > TABLE_TEXT_LIMIT:
            raise ValueError(
                f'the table has more than {TABLE_TEXT_LIMIT} characters'
            )
        return self

    def text_length(self) -> int:
        return sum(len(cell) for row in self.rows for cell in row)

    def to_block(self) -> dict[str, typing.Any]:
        block: dict[str, typing.Any] = {
            'type': 'table',
            'rows': [
                [{'type': 'raw_text', 'text': cell} for cell in row]
                for row in self.rows
            ],
        }
        if self.align:
            block['column_settings'] = [
                {'align': align} if align else None for align in self.align
            ]
        return block


BlockSpec = typing.Annotated[
    MarkdownSpec
    | HeaderSpec
    | DividerSpec
    | ContextSpec
    | ImageSpec
    | TableSpec,
    pydantic.Field(discriminator='type'),
]


def check_message(specs: list[BlockSpec]) -> None:
    """Check the limits for all blocks in one message.

    Raises :class:`ValueError` with a message for the agent.

    """
    if not 1 <= len(specs) <= MESSAGE_BLOCK_LIMIT:
        raise ValueError(
            f'a message must have 1 to {MESSAGE_BLOCK_LIMIT} blocks'
        )
    markdown = sum(
        len(spec.text) for spec in specs if isinstance(spec, MarkdownSpec)
    )
    if markdown > MARKDOWN_TEXT_LIMIT:
        raise ValueError(
            f'markdown blocks have more than {MARKDOWN_TEXT_LIMIT} '
            'characters in total'
        )
    tables = sum(
        spec.text_length() for spec in specs if isinstance(spec, TableSpec)
    )
    if tables > TABLE_TEXT_LIMIT:
        raise ValueError(
            f'tables have more than {TABLE_TEXT_LIMIT} characters in total'
        )


def to_blocks(specs: list[BlockSpec]) -> list[dict[str, typing.Any]]:
    """Check ``specs`` and return the Block Kit JSON."""
    check_message(specs)
    return [spec.to_block() for spec in specs]
