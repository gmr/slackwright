import json
import typing
import unittest

from slackwright import forms, toolkit
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

    async def test_post_limit_counts_messages(self) -> None:
        kit = self._kit(max_posts=1)
        result = await kit.call(
            'slack_post_markdown', {'text': 'a' * 7000 + '\n\n' + 'b' * 7000}
        )
        self.assertIn('post limit', result.text)
        self.assertEqual(self.slack.calls, [])

    async def test_failed_part_releases_parts_not_tried(self) -> None:
        self.slack.responses['chat.postMessage'] = [
            {'ok': True, 'ts': '1.1'},
            {'ok': False, 'error': 'ratelimited'},
            {'ok': True, 'ts': '1.2'},
        ]
        kit = self._kit(max_posts=5)
        text = '\n\n'.join(c * 7000 for c in 'abcde')
        failed = await kit.call('slack_post_markdown', {'text': text})
        retry = await kit.call('slack_post_markdown', {'text': 'f'})
        self.assertTrue(failed.is_error)
        self.assertFalse(retry.is_error)

    async def test_bad_message_does_not_use_the_limit(self) -> None:
        kit = self._kit(max_posts=1)
        bad = await kit.call(
            'slack_post_blocks',
            {
                'fallback_text': 'x',
                'blocks': [{'type': 'markdown', 'text': 'x' * 7000}] * 2,
            },
        )
        good = await kit.call('slack_post_markdown', {'text': 'a'})
        self.assertIn('markdown blocks', bad.text)
        self.assertFalse(good.is_error)

    async def test_neutralized_context_is_validated(self) -> None:
        result = await self._kit().call(
            'slack_post_blocks',
            {
                'fallback_text': 'x',
                'blocks': [
                    {'type': 'context', 'elements': ['x' * 2993 + '<!here>']}
                ],
            },
        )
        self.assertTrue(result.is_error)
        self.assertIn('elements.0', result.text)
        self.assertEqual(self.slack.calls, [])

    async def test_connection_error(self) -> None:
        await self.slack.__aexit__()
        result = await self._kit().call('slack_post_markdown', {'text': 'a'})
        self.assertTrue(result.is_error)
        self.assertIn('cannot connect to Slack', result.text)

    async def test_slack_error(self) -> None:
        self.slack.responses['chat.postMessage'] = {
            'ok': False,
            'error': 'not_in_channel',
        }
        result = await self._kit().call('slack_post_markdown', {'text': 'a'})
        self.assertTrue(result.is_error)
        self.assertIn('not_in_channel', result.text)


def _runtime() -> forms.Runtime:
    async def on_submit(submission: forms.Submission) -> None:
        return None

    return forms.Runtime(forms.MemoryFormStore(), on_submit)


FORM = {
    'title': 'Lunch',
    'intro': 'Pick one.',
    'fields': [
        {
            'type': 'radio',
            'id': 'meal',
            'label': 'Meal',
            'options': ['Soup', 'Salad'],
        },
    ],
}


class FeatureSchemaTestCase(unittest.TestCase):
    def test_features_add_tools(self) -> None:
        kit = toolkit.Toolkit(
            None,
            channel_id='C1',
            thread_ts='1.0',
            user_id='U1',
            forms=_runtime(),
            canvases=True,
        )
        tools = kit.anthropic()
        self.assertEqual(
            [tool['name'] for tool in tools],
            [
                'slack_post_markdown',
                'slack_post_blocks',
                'slack_create_form',
                'slack_canvas_create',
                'slack_canvas_edit',
                'slack_canvas_read',
            ],
        )
        for tool in tools:
            self.assertGreater(len(tool['description']), 40)
            for name, prop in _walk_properties(tool['input_schema']):
                if name == 'type':
                    continue
                with self.subTest(tool=tool['name'], field=name):
                    self.assertIn('description', prop)


class FeatureCallTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.slack = await fake_slack.FakeSlack().__aenter__()
        self.runtime = _runtime()

    async def asyncTearDown(self) -> None:
        await self.slack.__aexit__()

    def _kit(self, **kwargs: typing.Any) -> toolkit.Toolkit:
        options: dict[str, typing.Any] = {
            'forms': self.runtime,
            'canvases': True,
        } | kwargs
        return toolkit.Toolkit(
            self.slack.client,
            channel_id='C1',
            thread_ts='1.0',
            user_id='U1',
            **options,
        )

    async def test_disabled_tools_are_unknown(self) -> None:
        kit = self._kit(forms=None, canvases=False)
        for name in ('slack_create_form', 'slack_canvas_read'):
            result = await kit.call(name, {})
            self.assertIn('unknown tool', result.text)

    async def test_create_form(self) -> None:
        kit = self._kit()
        result = await kit.call('slack_create_form', FORM)
        self.assertFalse(result.is_error, result.text)
        data = json.loads(result.text)
        self.assertIn('later turn', data['note'])
        post = self.slack.payloads('chat.postMessage')[0]
        self.assertEqual(post['thread_ts'], '1.0')
        self.assertEqual(
            post['blocks'][-1]['elements'][0]['value'], data['form_id']
        )

        second = await kit.call('slack_create_form', FORM)
        self.assertIn('form limit reached', second.text)

    async def test_create_form_problems(self) -> None:
        kit = self._kit()
        result = await kit.call(
            'slack_create_form',
            FORM
            | {
                'fields': [
                    {'type': 'text', 'id': 'pw', 'label': 'Your password'}
                ]
            },
        )
        self.assertTrue(result.is_error)
        self.assertIn('secrets', result.text)
        self.assertEqual(self.slack.calls, [])

    async def test_form_counts_as_post(self) -> None:
        kit = self._kit(max_posts=1, max_forms=2)
        await kit.call('slack_post_markdown', {'text': 'hi'})
        result = await kit.call('slack_create_form', FORM)
        self.assertIn('post limit', result.text)

    async def test_canvas_create(self) -> None:
        self.slack.responses['canvases.create'] = {
            'ok': True,
            'canvas_id': 'F1',
        }
        kit = self._kit()
        result = await kit.call(
            'slack_canvas_create',
            {'title': 'Notes', 'markdown': '# Notes <!here>'},
        )
        self.assertEqual(json.loads(result.text)['canvas_id'], 'F1')
        created = self.slack.payloads('canvases.create')[0]
        self.assertEqual(created['channel_id'], 'C1')
        self.assertEqual(
            created['document_content']['markdown'], '# Notes <\u200b!here>'
        )
        # A canvas that this toolkit created needs no share check.
        self.slack.responses['canvases.getContent'] = {
            'ok': True,
            'content': '# Notes',
        }
        read = await kit.call('slack_canvas_read', {'canvas_id': 'F1'})
        self.assertEqual(json.loads(read.text)['markdown'], '# Notes')
        self.assertNotIn('files.info', self.slack.methods())

    async def test_channel_canvas_exists(self) -> None:
        self.slack.responses['conversations.canvases.create'] = {
            'ok': False,
            'error': 'channel_canvas_already_exists',
        }
        result = await self._kit().call(
            'slack_canvas_create',
            {'title': 'Notes', 'markdown': '# Notes', 'channel_canvas': True},
        )
        self.assertTrue(result.is_error)
        self.assertIn('already has a channel canvas', result.text)

    async def test_canvas_access(self) -> None:
        self.slack.responses['files.info'] = {
            'ok': True,
            'file': {'channels': ['C9']},
        }
        self.slack.responses['canvases.getContent'] = {
            'ok': True,
            'content': 'x' * (toolkit.CANVAS_READ_LIMIT + 1),
        }
        denied = await self._kit().call(
            'slack_canvas_read', {'canvas_id': 'F2'}
        )
        self.assertIn('not shared with this channel', denied.text)

        async def can_post(channel_id: str, user_id: str) -> bool:
            return channel_id == 'C9'

        allowed = await self._kit(can_post=can_post).call(
            'slack_canvas_read', {'canvas_id': 'F2'}
        )
        data = json.loads(allowed.text)
        self.assertTrue(data['truncated'])
        self.assertEqual(len(data['markdown']), toolkit.CANVAS_READ_LIMIT)

    async def test_canvas_edit(self) -> None:
        self.slack.responses['files.info'] = {
            'ok': True,
            'file': {'channels': ['C1']},
        }
        self.slack.responses['canvases.sections.lookup'] = [
            {'ok': True, 'sections': []},
            {'ok': True, 'sections': [{'id': 'temp:1'}]},
        ]
        kit = self._kit(max_posts=1)
        missing = await kit.call(
            'slack_canvas_edit',
            {
                'canvas_id': 'F3',
                'operation': 'replace',
                'markdown': 'new',
                'section_contains': 'Old',
            },
        )
        self.assertIn('0 sections', missing.text)
        # The failed lookup did not use up the post limit.
        result = await kit.call(
            'slack_canvas_edit',
            {
                'canvas_id': 'F3',
                'operation': 'replace',
                'markdown': 'new',
                'section_contains': 'Old',
            },
        )
        self.assertFalse(result.is_error, result.text)
        self.assertEqual(len(self.slack.payloads('canvases.edit')), 1)

    async def test_canvas_edit_input_rules(self) -> None:
        kit = self._kit()
        for arguments, message in (
            (
                {'operation': 'replace', 'markdown': 'x'},
                'needs section_contains',
            ),
            ({'operation': 'insert_at_end'}, 'needs markdown'),
        ):
            result = await kit.call(
                'slack_canvas_edit', {'canvas_id': 'F1', **arguments}
            )
            self.assertIn(message, result.text)
        self.assertEqual(self.slack.calls, [])

    async def test_form_intro_follows_broadcast_setting(self) -> None:
        form = FORM | {'intro': '<!here> pick one'}
        await self._kit().call('slack_create_form', form)
        await self._kit(allow_broadcasts=True).call('slack_create_form', form)
        intros = [
            post['blocks'][1]['text']
            for post in self.slack.payloads('chat.postMessage')
        ]
        self.assertEqual(
            intros, ['<\u200b!here> pick one', '<!here> pick one']
        )

    async def test_neutralized_form_intro_is_validated(self) -> None:
        kit = self._kit()
        bad = await kit.call(
            'slack_create_form', FORM | {'intro': 'a' * 1993 + '<!here>'}
        )
        self.assertTrue(bad.is_error)
        self.assertIn('intro', bad.text)
        self.assertEqual(self.slack.calls, [])
        # The rejected form did not use up the form limit.
        good = await kit.call('slack_create_form', FORM)
        self.assertFalse(good.is_error, good.text)

    async def test_canvas_check_is_remembered(self) -> None:
        self.slack.responses['files.info'] = {
            'ok': True,
            'file': {'channels': ['C1']},
        }
        self.slack.responses['canvases.getContent'] = {
            'ok': True,
            'content': '# Doc',
        }
        kit = self._kit()
        for _ in range(3):
            result = await kit.call('slack_canvas_read', {'canvas_id': 'F4'})
            self.assertFalse(result.is_error, result.text)
        self.assertEqual(self.slack.methods().count('files.info'), 1)
