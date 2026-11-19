import unittest

import pydantic

from slackwright import blocks

_ADAPTER: pydantic.TypeAdapter[list[blocks.BlockSpec]] = pydantic.TypeAdapter(
    list[blocks.BlockSpec]
)


def _specs(data: list[dict[str, object]]) -> list[blocks.BlockSpec]:
    return _ADAPTER.validate_python(data)


class ToBlocksTestCase(unittest.TestCase):
    def test_all_block_types(self) -> None:
        specs = _specs(
            [
                {'type': 'header', 'text': 'Deploys'},
                {'type': 'markdown', 'text': '**ok**'},
                {'type': 'divider'},
                {'type': 'context', 'elements': ['*a*', 'b']},
                {
                    'type': 'image',
                    'image_url': 'https://example.com/a.png',
                    'alt_text': 'chart',
                    'title': 'Chart',
                },
                {
                    'type': 'table',
                    'rows': [['Service', 'State'], ['api', 'ok']],
                    'align': ['left', None],
                },
            ]
        )
        self.assertEqual(
            blocks.to_blocks(specs),
            [
                {
                    'type': 'header',
                    'text': {
                        'type': 'plain_text',
                        'text': 'Deploys',
                        'emoji': True,
                    },
                },
                {'type': 'markdown', 'text': '**ok**'},
                {'type': 'divider'},
                {
                    'type': 'context',
                    'elements': [
                        {'type': 'mrkdwn', 'text': '*a*'},
                        {'type': 'mrkdwn', 'text': 'b'},
                    ],
                },
                {
                    'type': 'image',
                    'image_url': 'https://example.com/a.png',
                    'alt_text': 'chart',
                    'title': {'type': 'plain_text', 'text': 'Chart'},
                },
                {
                    'type': 'table',
                    'rows': [
                        [
                            {'type': 'raw_text', 'text': 'Service'},
                            {'type': 'raw_text', 'text': 'State'},
                        ],
                        [
                            {'type': 'raw_text', 'text': 'api'},
                            {'type': 'raw_text', 'text': 'ok'},
                        ],
                    ],
                    'column_settings': [{'align': 'left'}, None],
                },
            ],
        )

    def test_unknown_type_is_rejected(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            _specs([{'type': 'button', 'text': 'x'}])

    def test_empty_context_element_is_rejected(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            _specs([{'type': 'context', 'elements': ['']}])

    def test_extra_field_is_rejected(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            _specs([{'type': 'divider', 'color': 'red'}])


class TableTestCase(unittest.TestCase):
    def _table(self, rows: list[list[str]], **extra: object) -> None:
        _specs([{'type': 'table', 'rows': rows, **extra}])

    def test_ragged_rows(self) -> None:
        with self.assertRaisesRegex(pydantic.ValidationError, 'same number'):
            self._table([['a', 'b'], ['c']])

    def test_too_many_cells(self) -> None:
        with self.assertRaisesRegex(pydantic.ValidationError, '1 to 20'):
            self._table([['x'] * 21])

    def test_too_many_characters(self) -> None:
        with self.assertRaisesRegex(pydantic.ValidationError, '10000'):
            self._table([['x' * 5001, 'y' * 5000]])

    def test_too_many_align_items(self) -> None:
        with self.assertRaisesRegex(pydantic.ValidationError, 'align'):
            self._table([['a']], align=['left', 'right'])


class CheckMessageTestCase(unittest.TestCase):
    def test_no_blocks(self) -> None:
        with self.assertRaisesRegex(ValueError, '1 to 50'):
            blocks.check_message([])

    def test_markdown_total(self) -> None:
        specs = _specs([{'type': 'markdown', 'text': 'x' * 7000}] * 2)
        with self.assertRaisesRegex(ValueError, 'markdown'):
            blocks.check_message(specs)

    def test_table_total(self) -> None:
        table = {'type': 'table', 'rows': [['x' * 6000]]}
        with self.assertRaisesRegex(ValueError, 'tables'):
            blocks.check_message(_specs([table, table]))
