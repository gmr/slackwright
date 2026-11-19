import json
import typing
import unittest

from slackwright import toolkit
from tests import fake_slack


def _walk_properties(
    schema: dict[str, typing.Any],
) -> typing.Iterator[tuple[str, dict[str, typing.Any]]]:
    """Yield every object property in a JSON schema, with $defs."""
    for definition in schema.get('$defs', {}).values():
        yield from _walk_properties(definition)
    yield from schema.get('properties', {}).items()


class SchemaTestCase(unittest.TestCase):
    def test_tool_names(self) -> None:
        kit = toolkit.Toolkit(
            None, channel_id='C1', thread_ts='1.0', user_id='U1'
        )
        self.assertEqual(
            [tool['name'] for tool in kit.anthropic()],
            ['slack_post_markdown', 'slack_post_blocks'],
        )

    def test_every_field_has_a_description(self) -> None:
        kit = toolkit.Toolkit(
            None, channel_id='C1', thread_ts='1.0', user_id='U1'
        )
        for tool in kit.anthropic():
            self.assertGreater(len(tool['description']), 40)
            for name, prop in _walk_properties(tool['input_schema']):
                if name == 'type':
                    continue
                with self.subTest(tool=tool['name'], field=name):
                    self.assertIn('description', prop)

    def test_channel_id_only_with_can_post(self) -> None:
        async def can_post(channel_id: str, user_id: str) -> bool:
            return True

        bound = toolkit.Toolkit(
            None, channel_id='C1', thread_ts='1.0', user_id='U1'
        )
        open_ = toolkit.Toolkit(
            None,
            channel_id='C1',
            thread_ts='1.0',
            user_id='U1',
            can_post=can_post,
        )
        for tool in bound.anthropic():
            self.assertNotIn('channel_id', tool['input_schema']['properties'])
        for tool in open_.anthropic():
            self.assertIn('channel_id', tool['input_schema']['properties'])


class CallTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.slack = await fake_slack.FakeSlack().__aenter__()
        self.allowed: list[tuple[str, str]] = []

    async def asyncTearDown(self) -> None:
        await self.slack.__aexit__()

    def _kit(self, *, open_: bool = False, **kwargs: int) -> toolkit.Toolkit:
        async def can_post(channel_id: str, user_id: str) -> bool:
            self.allowed.append((channel_id, user_id))
            return channel_id == 'C2'

        return toolkit.Toolkit(
            self.slack.client,
            channel_id='C1',
            thread_ts='1.0',
            user_id='U1',
            can_post=can_post if open_ else None,
            **kwargs,
        )

    async def test_post_markdown(self) -> None:
        result = await self._kit().call(
            'slack_post_markdown', {'text': '**hi**'}
        )
        self.assertFalse(result.is_error)
        self.assertEqual(len(json.loads(result.text)['ts']), 1)
        self.assertEqual(
            self.slack.payloads('chat.postMessage'),
            [{'channel': 'C1', 'thread_ts': '1.0', 'markdown_text': '**hi**'}],
        )

    async def test_post_blocks(self) -> None:
        result = await self._kit().call(
            'slack_post_blocks',
            {
                'fallback_text': 'Status',
                'blocks': [{'type': 'header', 'text': 'Status'}],
            },
        )
        self.assertFalse(result.is_error, result.text)
        post = self.slack.payloads('chat.postMessage')[0]
        self.assertEqual(post['text'], 'Status')
        self.assertEqual(post['blocks'][0]['type'], 'header')

    async def test_broadcasts_neutralized_by_default(self) -> None:
        kit = self._kit()
        await kit.call('slack_post_markdown', {'text': '<!here> hi'})
        await kit.call(
            'slack_post_blocks',
            {
                'fallback_text': '<!channel>',
                'blocks': [
                    {'type': 'markdown', 'text': '<!everyone>'},
                    {'type': 'context', 'elements': ['<!subteam^S1|@ops>']},
                ],
            },
        )
        markdown, blocks = self.slack.payloads('chat.postMessage')
        self.assertEqual(markdown['markdown_text'], '<\u200b!here> hi')
        self.assertEqual(blocks['text'], '<\u200b!channel>')
        self.assertEqual(blocks['blocks'][0]['text'], '<\u200b!everyone>')
        self.assertEqual(
            blocks['blocks'][1]['elements'][0]['text'],
            '<\u200b!subteam^S1|@ops>',
        )

    async def test_allow_broadcasts(self) -> None:
        kit = toolkit.Toolkit(
            self.slack.client,
            channel_id='C1',
            thread_ts='1.0',
            user_id='U1',
            allow_broadcasts=True,
        )
        await kit.call('slack_post_markdown', {'text': '<!here> hi'})
        self.assertEqual(
            self.slack.payloads('chat.postMessage')[0]['markdown_text'],
            '<!here> hi',
        )

    async def test_unknown_tool(self) -> None:
        result = await self._kit().call('slack_nope', {})
        self.assertTrue(result.is_error)
        self.assertIn('unknown tool', result.text)

    async def test_validation_error_names_the_field(self) -> None:
        result = await self._kit().call(
            'slack_post_blocks',
            {
                'fallback_text': 'x',
                'blocks': [{'type': 'table', 'rows': [['a', 'b'], ['c']]}],
            },
        )
        self.assertTrue(result.is_error)
        error = json.loads(result.text)['error']
        self.assertIn('blocks.0.table', error)
        self.assertIn('same number', error)
        self.assertEqual(self.slack.calls, [])

    async def test_message_limit_error(self) -> None:
        result = await self._kit().call(
            'slack_post_blocks',
            {
                'fallback_text': 'x',
                'blocks': [{'type': 'markdown', 'text': 'x' * 7000}] * 2,
            },
        )
        self.assertTrue(result.is_error)
        self.assertIn('markdown blocks', result.text)

    async def test_other_channel_without_can_post(self) -> None:
        result = await self._kit().call(
            'slack_post_markdown', {'text': 'hi', 'channel_id': 'C2'}
        )
        self.assertTrue(result.is_error)
        self.assertEqual(self.slack.calls, [])

    async def test_other_channel_with_can_post(self) -> None:
        kit = self._kit(open_=True)
        allowed = await kit.call(
            'slack_post_markdown', {'text': 'hi', 'channel_id': 'C2'}
        )
        denied = await kit.call(
            'slack_post_markdown', {'text': 'hi', 'channel_id': 'C3'}
        )
        self.assertFalse(allowed.is_error)
        self.assertTrue(denied.is_error)
        self.assertEqual(self.allowed, [('C2', 'U1'), ('C3', 'U1')])
        self.assertEqual(
            self.slack.payloads('chat.postMessage'),
            [{'channel': 'C2', 'markdown_text': 'hi'}],
        )

    async def test_post_limit(self) -> None:
        kit = self._kit(max_posts=1)
        first = await kit.call('slack_post_markdown', {'text': 'a'})
        second = await kit.call('slack_post_markdown', {'text': 'b'})
        self.assertFalse(first.is_error)
        self.assertTrue(second.is_error)
        self.assertIn('post limit', second.text)

    async def test_slack_error(self) -> None:
        self.slack.responses['chat.postMessage'] = {
            'ok': False,
            'error': 'not_in_channel',
        }
        result = await self._kit().call('slack_post_markdown', {'text': 'a'})
        self.assertTrue(result.is_error)
        self.assertIn('not_in_channel', result.text)
