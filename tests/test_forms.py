import typing
import unittest

import pydantic

from slackwright import blocks as block_specs
from slackwright import forms
from tests import fake_slack


def spec(**overrides: typing.Any) -> forms.FormSpec:
    data: dict[str, typing.Any] = {
        'title': 'Deploy window',
        'intro': 'Pick a time. <!here>',
        'fields': [
            {'type': 'date', 'id': 'day', 'label': 'Day'},
            {
                'type': 'select',
                'id': 'slot',
                'label': 'Slot',
                'options': ['Morning', 'Afternoon'],
            },
            {
                'type': 'text',
                'id': 'notes',
                'label': 'Notes',
                'required': False,
            },
        ],
    }
    return forms.FormSpec.model_validate(data | overrides)


def field(data: dict[str, typing.Any]) -> typing.Any:
    return spec(fields=[data]).fields[0]


class Ack:
    def __init__(self) -> None:
        self.calls: list[dict[str, typing.Any]] = []

    async def __call__(self, **kwargs: typing.Any) -> None:
        self.calls.append(kwargs)


def answers(**values: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Return a ``state`` payload, keyed by field ID."""
    return {'values': {key: {'value': value} for key, value in values.items()}}


GOOD = answers(
    day={'type': 'datepicker', 'selected_date': '2026-12-01'},
    slot={'selected_option': {'value': 'Morning'}},
    notes={'value': None},
)


class FieldTestCase(unittest.TestCase):
    def test_elements(self) -> None:
        cases: list[tuple[dict[str, typing.Any], dict[str, typing.Any]]] = [
            (
                {
                    'type': 'text',
                    'multiline': True,
                    'max_length': 100,
                    'placeholder': 'Why?',
                    'initial': 'Because',
                },
                {
                    'type': 'plain_text_input',
                    'multiline': True,
                    'max_length': 100,
                    'initial_value': 'Because',
                    'placeholder': forms._plain('Why?'),
                },
            ),
            (
                {
                    'type': 'number',
                    'decimal': True,
                    'min': 0,
                    'max': 2.5,
                    'initial': 1,
                },
                {
                    'type': 'number_input',
                    'is_decimal_allowed': True,
                    'min_value': '0',
                    'max_value': '2.5',
                    'initial_value': '1',
                },
            ),
            ({'type': 'email'}, {'type': 'email_text_input'}),
            (
                {'type': 'url', 'initial': 'https://a.example'},
                {
                    'type': 'url_text_input',
                    'initial_value': 'https://a.example',
                },
            ),
            (
                {'type': 'date', 'initial': '2026-01-02'},
                {'type': 'datepicker', 'initial_date': '2026-01-02'},
            ),
            (
                {'type': 'time', 'initial': '09:30'},
                {'type': 'timepicker', 'initial_time': '09:30'},
            ),
            (
                {
                    'type': 'multi_select',
                    'options': ['a', 'b'],
                    'max_selected': 2,
                    'initial': ['b'],
                },
                {
                    'type': 'multi_static_select',
                    'options': forms._options(['a', 'b']),
                    'max_selected_items': 2,
                    'initial_options': forms._options(['b']),
                },
            ),
            (
                {
                    'type': 'radio',
                    'options': ['a', 'b'],
                    'initial': 'a',
                    'placeholder': 'ignored',
                },
                {
                    'type': 'radio_buttons',
                    'options': forms._options(['a', 'b']),
                    'initial_option': forms._options(['a'])[0],
                },
            ),
            (
                {'type': 'checkboxes', 'options': ['a']},
                {'type': 'checkboxes', 'options': forms._options(['a'])},
            ),
            (
                {'type': 'user', 'initial': 'U1'},
                {'type': 'users_select', 'initial_user': 'U1'},
            ),
            ({'type': 'users'}, {'type': 'multi_users_select'}),
            (
                {'type': 'channel', 'initial': 'C1'},
                {'type': 'conversations_select', 'initial_conversation': 'C1'},
            ),
            (
                {'type': 'channels', 'initial': ['C1', 'G2']},
                {
                    'type': 'multi_conversations_select',
                    'initial_conversations': ['C1', 'G2'],
                },
            ),
        ]
        for data, element in cases:
            with self.subTest(type=data['type']):
                block = field({'id': 'f', 'label': 'F', **data}).block()
                self.assertEqual(
                    block['element'], {'action_id': 'value', **element}
                )

    def test_input_block(self) -> None:
        block = field(
            {
                'type': 'email',
                'id': 'mail',
                'label': 'Mail',
                'required': False,
                'hint': 'Work address',
            }
        ).block()
        self.assertEqual(block['type'], 'input')
        self.assertEqual(block['block_id'], 'mail')
        self.assertTrue(block['optional'])
        self.assertEqual(block['hint'], forms._plain('Work address'))

    def test_invalid_fields(self) -> None:
        cases = {
            'radio allows at most 10': {
                'type': 'radio',
                'options': [str(i) for i in range(11)],
            },
            'options must be unique': {
                'type': 'select',
                'options': ['a', 'a'],
            },
            'initial must be one of': {
                'type': 'select',
                'options': ['a'],
                'initial': 'b',
            },
            'initial must be a list': {
                'type': 'checkboxes',
                'options': ['a'],
                'initial': 'a',
            },
            'max_selected is only': {
                'type': 'select',
                'options': ['a'],
                'max_selected': 1,
            },
            'initial must match YYYY-MM-DD': {
                'type': 'date',
                'initial': '12/01/2026',
            },
            'not a channel': {'type': 'channel', 'initial': 'U1'},
            'initial must be one ID': {'type': 'user', 'initial': ['U1']},
            'cannot ask for secrets': {
                'type': 'text',
                'hint': 'Paste your API key',
            },
            'should match pattern': {'type': 'text', 'id': 'Bad-ID'},
        }
        for message, data in cases.items():
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(pydantic.ValidationError, message),
            ):
                field({'id': 'f', 'label': 'F', **data})

    def test_invalid_form(self) -> None:
        with self.assertRaisesRegex(pydantic.ValidationError, 'unique'):
            spec(fields=[{'type': 'text', 'id': 'a', 'label': 'A'}] * 2)
        with self.assertRaisesRegex(pydantic.ValidationError, 'secrets'):
            spec(title='Reset password')
        with self.assertRaisesRegex(pydantic.ValidationError, '24'):
            spec(title='x' * 25)


class ConvertTestCase(unittest.TestCase):
    def check(
        self, data: dict[str, typing.Any], state: dict[str, typing.Any]
    ) -> typing.Any:
        return field({'id': 'f', 'label': 'F', **data}).convert(state)

    def test_values(self) -> None:
        cases: list[
            tuple[dict[str, typing.Any], dict[str, typing.Any], typing.Any]
        ] = [
            ({'type': 'text'}, {'value': 'hi'}, 'hi'),
            ({'type': 'number'}, {'value': '42'}, 42),
            ({'type': 'number', 'decimal': True}, {'value': '1.5'}, 1.5),
            ({'type': 'email'}, {'value': ' a@b.example '}, 'a@b.example'),
            (
                {'type': 'url'},
                {'value': 'https://a.example'},
                'https://a.example',
            ),
            ({'type': 'time'}, {'selected_time': '13:05'}, '13:05'),
            (
                {'type': 'radio', 'options': ['a']},
                {'selected_option': {'value': 'a'}},
                'a',
            ),
            (
                {'type': 'checkboxes', 'options': ['a', 'b']},
                {'selected_options': [{'value': 'a'}, {'value': 'b'}]},
                ['a', 'b'],
            ),
            ({'type': 'user'}, {'selected_user': 'U1'}, 'U1'),
            (
                {'type': 'users'},
                {'selected_users': ['U1', 'U2']},
                ['U1', 'U2'],
            ),
            ({'type': 'channel'}, {'selected_conversation': 'C1'}, 'C1'),
            ({'type': 'channels', 'required': False}, {}, []),
            ({'type': 'text', 'required': False}, {'value': ''}, None),
        ]
        for data, state, expected in cases:
            with self.subTest(type=data['type'], state=state):
                self.assertEqual(self.check(data, state), expected)

    def test_problems(self) -> None:
        cases: list[
            tuple[dict[str, typing.Any], dict[str, typing.Any], str]
        ] = [
            ({'type': 'text'}, {}, 'required'),
            ({'type': 'text', 'min_length': 5}, {'value': 'hi'}, 'at least 5'),
            (
                {'type': 'text', 'max_length': 2},
                {'value': 'hello'},
                'at most 2',
            ),
            ({'type': 'number'}, {'value': '1.5'}, 'whole number'),
            ({'type': 'number', 'decimal': True}, {'value': 'x'}, 'a number'),
            ({'type': 'number', 'min': 10}, {'value': '5'}, '10 or more'),
            (
                {'type': 'number', 'max': 1.5, 'decimal': True},
                {'value': '2'},
                '1.5 or less',
            ),
            ({'type': 'email'}, {'value': 'nope'}, 'email'),
            ({'type': 'url'}, {'value': 'ftp://x'}, 'https://'),
            ({'type': 'date'}, {'selected_date': 'tomorrow'}, 'YYYY-MM-DD'),
            (
                {'type': 'select', 'options': ['a']},
                {'selected_option': {'value': 'forged'}},
                'Choose from',
            ),
            (
                {
                    'type': 'multi_select',
                    'options': ['a', 'b'],
                    'max_selected': 1,
                },
                {'selected_options': [{'value': 'a'}, {'value': 'b'}]},
                'at most 1',
            ),
            ({'type': 'users'}, {'selected_users': []}, 'required'),
        ]
        for data, state, message in cases:
            with (
                self.subTest(type=data['type'], state=state),
                self.assertRaisesRegex(ValueError, message),
            ):
                self.check(data, state)


class RuntimeTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.slack = await fake_slack.FakeSlack().__aenter__()
        self.store = forms.MemoryFormStore()
        self.submissions: list[forms.Submission] = []

        async def on_submit(submission: forms.Submission) -> None:
            self.submissions.append(submission)

        self.runtime = forms.Runtime(self.store, on_submit)

    async def asyncTearDown(self) -> None:
        await self.slack.__aexit__()

    async def post(self, **overrides: typing.Any) -> forms.FormRecord:
        return await self.runtime.post(
            self.slack.client,
            spec(**overrides),
            channel_id='C1',
            thread_ts='1.0',
            requester='U1',
        )

    def click(
        self,
        record: forms.FormRecord | None,
        action_id: str,
        *,
        user: str = 'U1',
        state: dict[str, typing.Any] | None = None,
    ) -> dict[str, typing.Any]:
        form_id = record.form_id if record else 'gone'
        return {
            'actions': [{'action_id': action_id, 'value': form_id}],
            'user': {'id': user},
            'channel': {'id': 'C1'},
            'trigger_id': 'T.1',
            'message': {
                'ts': record.message_ts if record else '9.0',
                'thread_ts': '1.0',
                'blocks': [
                    {'type': 'header'},
                    {'type': 'input'},
                    {'type': 'actions'},
                ],
            },
            'state': state or {},
        }

    def submit(
        self,
        form_id: str,
        state: dict[str, typing.Any],
        *,
        user: str = 'U1',
    ) -> dict[str, typing.Any]:
        return {
            'user': {'id': user},
            'view': {
                'private_metadata': form_id,
                'blocks': [
                    {'type': 'context'},
                    {'type': 'input', 'block_id': 'day'},
                ],
                'state': state,
            },
        }

    async def test_post_modal(self) -> None:
        record = await self.post()
        stored = await self.store.get(record.form_id)
        self.assertEqual(stored and stored.message_ts, record.message_ts)
        post = self.slack.payloads('chat.postMessage')[0]
        self.assertEqual(post['text'], 'Form: Deploy window')
        self.assertEqual(
            [block['type'] for block in post['blocks']],
            ['header', 'markdown', 'context', 'actions'],
        )
        self.assertEqual(post['blocks'][1]['text'], 'Pick a time. <!here>')
        button = post['blocks'][3]['elements'][0]
        self.assertEqual(button['action_id'], forms.ACTION_OPEN)
        self.assertEqual(button['value'], record.form_id)

    async def test_open_and_submit(self) -> None:
        record = await self.post()
        ack = Ack()
        await self.runtime.handle_action(
            ack, self.click(record, forms.ACTION_OPEN), self.slack.client
        )
        self.assertEqual(ack.calls, [{}])
        opened = self.slack.payloads('views.open')[0]
        self.assertEqual(opened['trigger_id'], 'T.1')
        view = opened['view']
        self.assertEqual(view['private_metadata'], record.form_id)
        self.assertEqual(view['callback_id'], forms.CALLBACK_ID)
        self.assertEqual(
            [block['type'] for block in view['blocks']],
            ['section', 'context', 'input', 'input', 'input'],
        )

        ack = Ack()
        await self.runtime.handle_view(
            ack, self.submit(record.form_id, GOOD), self.slack.client
        )
        self.assertEqual(ack.calls, [{}])
        self.assertEqual(len(self.submissions), 1)
        submission = self.submissions[0]
        self.assertEqual(
            submission.values,
            {'day': '2026-12-01', 'slot': 'Morning', 'notes': None},
        )
        self.assertEqual(submission.user_id, 'U1')
        self.assertEqual(submission.thread_ts, '1.0')
        update = self.slack.payloads('chat.update')[0]
        self.assertEqual(update['ts'], record.message_ts)
        self.assertEqual(
            update['blocks'][-1]['elements'][0]['text'], 'Answered by <@U1>'
        )

        ack = Ack()
        await self.runtime.handle_view(
            ack, self.submit(record.form_id, GOOD), self.slack.client
        )
        self.assertEqual(ack.calls[0]['response_action'], 'update')
        self.assertEqual(len(self.submissions), 1)

    async def test_submit_problems(self) -> None:
        record = await self.post()
        ack = Ack()
        await self.runtime.handle_view(
            ack,
            self.submit(
                record.form_id,
                answers(slot={'selected_option': {'value': 'Night'}}),
            ),
            self.slack.client,
        )
        self.assertEqual(
            ack.calls,
            [
                {
                    'response_action': 'errors',
                    'errors': {
                        'day': 'This field is required.',
                        'slot': 'Choose from the options.',
                    },
                }
            ],
        )
        self.assertEqual(self.submissions, [])

    async def test_submit_expired_or_not_allowed(self) -> None:
        record = await self.post()
        for form_id, user, message in (
            ('gone', 'U1', 'This form has expired.'),
            (record.form_id, 'U2', 'You cannot answer this form.'),
        ):
            ack = Ack()
            await self.runtime.handle_view(
                ack, self.submit(form_id, GOOD, user=user), self.slack.client
            )
            self.assertEqual(
                ack.calls,
                [{'response_action': 'errors', 'errors': {'day': message}}],
            )
        self.assertEqual(self.submissions, [])

    async def test_open_not_allowed(self) -> None:
        record = await self.post(respondents=['U3'])
        await self.runtime.handle_action(
            Ack(),
            self.click(record, forms.ACTION_OPEN, user='U1'),
            self.slack.client,
        )
        self.assertNotIn('views.open', self.slack.methods())
        ephemeral = self.slack.payloads('chat.postEphemeral')[0]
        self.assertEqual(ephemeral['user'], 'U1')
        self.assertIn('cannot answer', ephemeral['text'])

    async def test_open_expired(self) -> None:
        await self.runtime.handle_action(
            Ack(), self.click(None, forms.ACTION_OPEN), self.slack.client
        )
        self.assertIn(
            'expired', self.slack.payloads('chat.postEphemeral')[0]['text']
        )
        update = self.slack.payloads('chat.update')[0]
        self.assertEqual(update['ts'], '9.0')
        self.assertEqual(
            [block['type'] for block in update['blocks']],
            ['header', 'context'],
        )

    async def test_inline(self) -> None:
        record = await self.post(mode='inline', respondents='anyone')
        post = self.slack.payloads('chat.postMessage')[0]
        self.assertEqual(
            [block['type'] for block in post['blocks']],
            [
                'header',
                'markdown',
                'input',
                'input',
                'input',
                'context',
                'actions',
            ],
        )
        self.assertEqual(
            post['blocks'][-1]['elements'][0]['action_id'],
            forms.ACTION_SUBMIT,
        )

        await self.runtime.handle_action(
            Ack(),
            self.click(
                record, forms.ACTION_SUBMIT, user='U2', state=answers()
            ),
            self.slack.client,
        )
        ephemeral = self.slack.payloads('chat.postEphemeral')[0]
        self.assertIn('• Day: This field is required.', ephemeral['text'])
        self.assertEqual(self.submissions, [])

        await self.runtime.handle_action(
            Ack(),
            self.click(record, forms.ACTION_SUBMIT, user='U2', state=GOOD),
            self.slack.client,
        )
        self.assertEqual(len(self.submissions), 1)
        summary = self.slack.payloads('chat.update')[0]['blocks']
        self.assertEqual(
            [block['type'] for block in summary],
            ['header', 'section', 'section', 'section', 'context'],
        )
        self.assertEqual(
            [block['text'] for block in summary[1:4]],
            [
                block_specs.plain_text('Day\n2026-12-01'),
                block_specs.plain_text('Slot\nMorning'),
                block_specs.plain_text('Notes\n-'),
            ],
        )

        await self.runtime.handle_action(
            Ack(),
            self.click(record, forms.ACTION_SUBMIT, user='U3', state=GOOD),
            self.slack.client,
        )
        self.assertIn(
            'already answered',
            self.slack.payloads('chat.postEphemeral')[-1]['text'],
        )
        self.assertEqual(len(self.submissions), 1)

    async def test_inline_summary_is_plain_text(self) -> None:
        record = await self.post(mode='inline', respondents='anyone')
        evil = '[x](https://evil.example) <!here>'
        state = answers(
            day={'type': 'datepicker', 'selected_date': '2026-12-01'},
            slot={'selected_option': {'value': 'Morning'}},
            notes={'value': evil},
        )
        await self.runtime.handle_action(
            Ack(),
            self.click(record, forms.ACTION_SUBMIT, user='U2', state=state),
            self.slack.client,
        )
        text = self.slack.payloads('chat.update')[0]['blocks'][3]['text']
        self.assertEqual(text['type'], 'plain_text')
        self.assertEqual(text['text'], f'Notes\n{evil}')

    async def test_inline_each_respondent_once(self) -> None:
        record = await self.post(
            mode='inline', respondents='anyone', single_submission=False
        )
        for user in ('U2', 'U3', 'U2'):
            await self.runtime.handle_action(
                Ack(),
                self.click(record, forms.ACTION_SUBMIT, user=user, state=GOOD),
                self.slack.client,
            )
        self.assertEqual([s.user_id for s in self.submissions], ['U2', 'U3'])
        self.assertNotIn('chat.update', self.slack.methods())
        texts = [p['text'] for p in self.slack.payloads('chat.postEphemeral')]
        self.assertEqual(texts[:2], ['Thanks. Your answers were sent.'] * 2)
        self.assertIn('already answered', texts[2])

    async def test_mismatched_actions_are_ignored(self) -> None:
        modal = await self.post()
        inline = await self.post(mode='inline')
        await self.runtime.handle_action(
            Ack(),
            self.click(modal, forms.ACTION_SUBMIT, state=GOOD),
            self.slack.client,
        )
        await self.runtime.handle_action(
            Ack(), self.click(inline, forms.ACTION_OPEN), self.slack.client
        )
        self.assertEqual(self.submissions, [])
        self.assertNotIn('views.open', self.slack.methods())

    async def test_update_failure_still_submits(self) -> None:
        record = await self.post()
        self.slack.responses['chat.update'] = {
            'ok': False,
            'error': 'message_not_found',
        }
        await self.runtime.handle_view(
            Ack(), self.submit(record.form_id, GOOD), self.slack.client
        )
        self.assertEqual(len(self.submissions), 1)

    def test_register(self) -> None:
        registered: list[tuple[str, typing.Any, typing.Any]] = []

        class App:
            def action(self, matcher: typing.Any) -> typing.Any:
                return lambda handler: registered.append(
                    ('action', matcher, handler)
                )

            def view(self, matcher: typing.Any) -> typing.Any:
                return lambda handler: registered.append(
                    ('view', matcher, handler)
                )

        self.runtime.register(App())
        self.assertEqual(
            registered,
            [
                ('action', forms.ACTION_PATTERN, self.runtime.handle_action),
                ('view', forms.CALLBACK_ID, self.runtime.handle_view),
            ],
        )
        self.assertTrue(forms.ACTION_PATTERN.match(forms.ACTION_OPEN))
        self.assertFalse(forms.ACTION_PATTERN.match('value'))
