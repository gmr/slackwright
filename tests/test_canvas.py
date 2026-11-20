import unittest

from slackwright import canvas
from tests import fake_slack


class CanvasTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_create(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            slack.responses['canvases.create'] = {
                'ok': True,
                'canvas_id': 'F1',
            }
            slack.responses['conversations.canvases.create'] = {
                'ok': True,
                'canvas_id': 'F2',
            }
            tab = await canvas.create(slack.client, 'C1', 'Notes', '# Hi')
            own = await canvas.create(
                slack.client, 'C1', 'Notes', '# Hi', channel_canvas=True
            )
        self.assertEqual((tab, own), ('F1', 'F2'))
        content = {'type': 'markdown', 'markdown': '# Hi'}
        self.assertEqual(
            slack.payloads('canvases.create'),
            [
                {
                    'title': 'Notes',
                    'document_content': content,
                    'channel_id': 'C1',
                }
            ],
        )
        self.assertEqual(
            slack.payloads('conversations.canvases.create'),
            [
                {
                    'channel_id': 'C1',
                    'document_content': content,
                    'title': 'Notes',
                }
            ],
        )

    async def test_create_channel_canvas_exists(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            slack.responses['conversations.canvases.create'] = {
                'ok': False,
                'error': 'channel_canvas_already_exists',
            }
            with self.assertRaisesRegex(ValueError, 'already has'):
                await canvas.create(
                    slack.client, 'C1', 'T', 'x', channel_canvas=True
                )

    async def test_edit(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            await canvas.edit(
                slack.client,
                'F1',
                'insert_after',
                '- step',
                section_id='temp:1',
            )
            await canvas.edit(slack.client, 'F1', 'rename', 'New title')
            await canvas.edit(
                slack.client, 'F1', 'delete', 'ignored', section_id='temp:2'
            )
        changes = [p['changes'][0] for p in slack.payloads('canvases.edit')]
        self.assertEqual(
            changes,
            [
                {
                    'operation': 'insert_after',
                    'section_id': 'temp:1',
                    'document_content': {
                        'type': 'markdown',
                        'markdown': '- step',
                    },
                },
                {
                    'operation': 'rename',
                    'title_content': {
                        'type': 'markdown',
                        'markdown': 'New title',
                    },
                },
                {'operation': 'delete', 'section_id': 'temp:2'},
            ],
        )

    async def test_edit_needs_values(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            with self.assertRaisesRegex(ValueError, 'needs section_id'):
                await canvas.edit(slack.client, 'F1', 'replace', 'x')
            with self.assertRaisesRegex(ValueError, 'needs markdown'):
                await canvas.edit(slack.client, 'F1', 'insert_at_end')
        self.assertEqual(slack.calls, [])

    async def test_find_section(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            slack.responses['canvases.sections.lookup'] = [
                {'ok': True, 'sections': [{'id': 'temp:1'}]},
                {'ok': True, 'sections': []},
                {'ok': True, 'sections': [{'id': 'a'}, {'id': 'b'}]},
            ]
            self.assertEqual(
                await canvas.find_section(slack.client, 'F1', 'Steps'),
                'temp:1',
            )
            for count in (0, 2):
                with self.assertRaisesRegex(
                    ValueError, f'{count} sections contain'
                ):
                    await canvas.find_section(slack.client, 'F1', 'Steps')
        lookup = slack.payloads('canvases.sections.lookup')[0]
        self.assertEqual(lookup['criteria'], '{"contains_text": "Steps"}')

    async def test_read_and_shares(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            slack.responses['canvases.getContent'] = {
                'ok': True,
                'content': '# Doc',
            }
            slack.responses['files.info'] = {
                'ok': True,
                'file': {
                    'channels': ['C1'],
                    'groups': ['G1'],
                    'shares': {'public': {'C2': []}, 'private': {'G2': []}},
                    'linked_channel_id': 'C3',
                },
            }
            self.assertEqual(await canvas.read(slack.client, 'F1'), '# Doc')
            self.assertEqual(
                await canvas.shared_with(slack.client, 'F1'),
                {'C1', 'G1', 'C2', 'G2', 'C3'},
            )
        self.assertEqual(
            slack.payloads('canvases.getContent'),
            [{'canvas_id': 'F1', 'content_type': 'markdown'}],
        )
