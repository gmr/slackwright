import json
import re
import time
import unittest

from slack_sdk import errors

from slackwright import render
from tests import fake_slack


def _long_answer() -> str:
    paragraph = 'Lorem ipsum dolor sit amet, consectetur adipiscing. ' * 20
    table = '| Name | Count |\n|---|---|\n' + '\n'.join(
        f'| row-{i} | {i} |' for i in range(200)
    )
    code = '```python\n' + 'print("hello")\n' * 300 + '```'
    parts = [f'## Section {i}\n\n{paragraph}' for i in range(24)]
    parts.insert(5, table)
    parts.insert(12, code)
    return '\n\n'.join(parts)


class NeutralizeBroadcastsTestCase(unittest.TestCase):
    ATTACKS = (
        '<!here>',
        '<!GROUP>',
        '<!subteam^S1|@devs>',
        '<!x|<!here>>',
        '<!date^1^x|<!here>>',
        '<!date^1392734382|today>',
        '<!DATE^1700000000^{date}|Nov 14>',
        '<!|>',
    )

    def test_special_mentions(self) -> None:
        for name in ('channel', 'here', 'everyone'):
            with self.subTest(name=name):
                self.assertEqual(
                    render.neutralize_broadcasts(f'hi <!{name}>!'),
                    f'hi <\u200b!{name}>!',
                )
                self.assertEqual(
                    render.neutralize_broadcasts(f'hi @{name}!'),
                    f'hi @\u200b{name}!',
                )

    def test_every_special_token(self) -> None:
        for text, expected in (
            ('<!here>', '<\u200b!here>'),
            ('<!GROUP>', '<\u200b!GROUP>'),
            ('<!subteam^S1|@devs>', '<\u200b!subteam^S1|@devs>'),
            ('<!channel|@channel>', '<\u200b!channel|@\u200bchannel>'),
            ('<!|>', '<\u200b!|>'),
        ):
            with self.subTest(text=text):
                self.assertEqual(render.neutralize_broadcasts(text), expected)

    def test_nested_label_bypass(self) -> None:
        result = render.neutralize_broadcasts('<!x|<!here>>')
        self.assertIsNone(re.search('<!', result))

    def test_date_label_bypass(self) -> None:
        result = render.neutralize_broadcasts('<!date^1^x|<!here>>')
        self.assertNotIn('<!here', result)

    def test_no_live_token_remains(self) -> None:
        for text in self.ATTACKS:
            with self.subTest(text=text):
                result = render.neutralize_broadcasts(text)
                self.assertIsNone(re.search(r'<!(?!date\^)', result))

    def test_bare_words(self) -> None:
        for text, expected in (
            ('_@here_', '_@\u200bhere_'),
            ('*@channel*', '*@\u200bchannel*'),
            ('@Everyone', '@\u200bEveryone'),
            ('@group', '@\u200bgroup'),
        ):
            with self.subTest(text=text):
                self.assertEqual(render.neutralize_broadcasts(text), expected)

    def test_other_text_is_unchanged(self) -> None:
        for text in (
            '<@U123> hi',
            'mail me at email@here.com',
            '@channels and @hereafter',
            '<!date^1700000000^{date}|Nov 14>',
            '<!date^1700000000^{date} at {time}^https://x.test|Nov 14>',
            'x@here.com',
            'plain text',
        ):
            with self.subTest(text=text):
                self.assertEqual(render.neutralize_broadcasts(text), text)


class SplitMarkdownTestCase(unittest.TestCase):
    def test_empty_text(self) -> None:
        self.assertEqual(render.split_markdown('  \n '), [])

    def test_short_text_is_one_part(self) -> None:
        self.assertEqual(render.split_markdown('**hi**\n'), ['**hi**'])

    def test_long_text_splits_between_blocks(self) -> None:
        text = _long_answer()
        self.assertGreater(len(text), 30_000)
        parts = render.split_markdown(text)
        self.assertGreater(len(parts), 2)
        for part in parts:
            self.assertLessEqual(len(part), render.MARKDOWN_LIMIT)
            self.assertEqual(part.count('```') % 2, 0)
        joined = '\n\n'.join(parts)
        self.assertEqual(joined.count('| row-'), 200)
        self.assertEqual(joined.count('print("hello")'), 300)
        self.assertEqual(joined.count('## Section'), 24)
        table = next(part for part in parts if '| row-0 |' in part)
        self.assertIn('| row-199 |', table)

    def test_block_over_limit_is_one_part(self) -> None:
        code = '```\n' + 'x' * (render.MARKDOWN_LIMIT + 10) + '\n```'
        parts = render.split_markdown(f'intro\n\n{code}\n\noutro')
        self.assertEqual(parts, ['intro', code, 'outro'])


class PostMarkdownTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_short_answer_is_one_message(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            timestamps = await render.post_markdown(
                slack.client, 'C1', '1.0', '# Hi\n\n**bold**'
            )
        self.assertEqual(len(timestamps), 1)
        self.assertEqual(
            slack.payloads('chat.postMessage'),
            [
                {
                    'channel': 'C1',
                    'thread_ts': '1.0',
                    'markdown_text': '# Hi\n\n**bold**',
                }
            ],
        )

    async def test_long_answer_is_split(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            timestamps = await render.post_markdown(
                slack.client, 'C1', '1.0', _long_answer()
            )
        posts = slack.payloads('chat.postMessage')
        self.assertEqual(len(timestamps), len(posts))
        self.assertGreater(len(posts), 2)
        for post in posts:
            self.assertLessEqual(
                len(post['markdown_text']), render.MARKDOWN_LIMIT
            )

    async def test_oversized_block_is_uploaded(self) -> None:
        code = '```\n' + 'x' * (render.MARKDOWN_LIMIT + 10) + '\n```'
        async with fake_slack.FakeSlack() as slack:
            timestamps = await render.post_markdown(
                slack.client, 'C1', '1.0', code
            )
        self.assertEqual(timestamps, [])
        self.assertIn('files.completeUploadExternal', slack.methods())
        self.assertEqual(slack.uploads, [code.encode()])


class StreamTestCase(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _chunks(payload: dict[str, object]) -> list[dict[str, object]]:
        chunks = payload.get('chunks') or []
        if isinstance(chunks, str):
            chunks = json.loads(chunks)
        assert isinstance(chunks, list)
        return chunks

    async def test_text_and_tasks(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            stream = render.Stream(
                slack.client, 'C1', '1.0', user_id='U1', team_id='T1'
            )
            await stream.text('Looking ')
            await stream.task('t1', 'Search', 'in_progress')
            await stream.task(
                't1',
                'Search',
                'complete',
                output='3 hits',
                sources=[('Doc', 'https://example.com')],
            )
            await stream.text('done.')
            self.assertEqual(await stream.close(), [])
            self.assertIsNotNone(stream.ts)
        self.assertEqual(
            slack.methods(),
            ['chat.startStream', 'chat.appendStream', 'chat.stopStream'],
        )
        start = slack.payloads('chat.startStream')[0]
        self.assertEqual(start['recipient_user_id'], 'U1')
        self.assertEqual(start['recipient_team_id'], 'T1')
        self.assertEqual(
            self._chunks(start),
            [
                {'type': 'markdown_text', 'text': 'Looking '},
                {
                    'type': 'task_update',
                    'id': 't1',
                    'title': 'Search',
                    'status': 'in_progress',
                },
            ],
        )
        append = self._chunks(slack.payloads('chat.appendStream')[0])
        self.assertEqual(append[0]['output'], '3 hits')
        self.assertEqual(
            append[0]['sources'],
            [{'type': 'url', 'text': 'Doc', 'url': 'https://example.com'}],
        )
        stop = self._chunks(slack.payloads('chat.stopStream')[0])
        self.assertEqual(stop, [{'type': 'markdown_text', 'text': 'done.'}])

    async def test_text_over_limit_is_posted_after(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            stream = render.Stream(slack.client, 'C1', '1.0')
            await stream.text('a' * 11_000)
            await stream.text('b' * 2_000)
            timestamps = await stream.close()
        self.assertEqual(len(timestamps), 1)
        self.assertEqual(slack.methods()[-1], 'chat.postMessage')
        self.assertEqual(
            slack.payloads('chat.postMessage')[0]['markdown_text'],
            'b' * 2_000,
        )

    async def test_expired_stream_marks_open_tasks(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            stream = render.Stream(slack.client, 'C1', '1.0', max_seconds=0)
            await stream.task('t1', 'Search', 'in_progress')
            await stream.task('t2', 'Ignored', 'in_progress')
            await stream.text('late text')
            await stream.close()
        self.assertEqual(
            slack.methods(),
            ['chat.startStream', 'chat.stopStream', 'chat.postMessage'],
        )
        stop = self._chunks(slack.payloads('chat.stopStream')[0])
        self.assertEqual(stop[0]['id'], 't1')
        self.assertEqual(stop[0]['status'], 'error')

    async def test_close_without_content(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            stream = render.Stream(slack.client, 'C1', '1.0')
            await stream.text('')
            self.assertEqual(await stream.close(), [])
            self.assertIsNone(stream.ts)
        self.assertEqual(slack.methods(), [])

    async def test_task_limit(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            stream = render.Stream(slack.client, 'C1', '1.0')
            for i in range(render.STREAM_MAX_TASKS + 1):
                await stream.task(f't{i}', 'Step', 'complete')
            await stream.close()
        ids = [
            chunk['id']
            for _, payload in slack.calls
            for chunk in self._chunks(payload)
        ]
        self.assertEqual(len(ids), render.STREAM_MAX_TASKS)

    async def test_text_after_overflow_stays_in_order(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            stream = render.Stream(slack.client, 'C1', '1.0')
            await stream.text('a' * 11_000)
            await stream.text('b' * 2_000)
            await stream.text('c' * 500)
            await stream.close()
        self.assertEqual(
            slack.payloads('chat.postMessage')[0]['markdown_text'],
            'b' * 2_000 + 'c' * 500,
        )

    async def test_failed_overflow_post_is_retried(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            slack.responses['chat.postMessage'] = [
                {'ok': True, 'ts': '1.1'},
                {'ok': False, 'error': 'ratelimited'},
                {'ok': True, 'ts': '1.2'},
            ]
            stream = render.Stream(slack.client, 'C1', '1.0')
            await stream.text('x' * 11_000)
            await stream.text('a' * 8_000 + '\n\n' + 'b' * 8_000)
            with self.assertRaises(errors.SlackApiError):
                await stream.close()
            self.assertEqual(await stream.close(), ['1.2'])
        posted = [
            p['markdown_text'] for p in slack.payloads('chat.postMessage')
        ]
        self.assertEqual(posted, ['a' * 8_000, 'b' * 8_000, 'b' * 8_000])

    async def test_failed_stop_is_retried(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            slack.responses['chat.stopStream'] = [
                {'ok': False, 'error': 'internal_error'},
                {'ok': True, 'ts': '1.1'},
            ]
            stream = render.Stream(slack.client, 'C1', '1.0')
            await stream.task('t1', 'Search', 'in_progress')
            with self.assertRaises(errors.SlackApiError):
                await stream.close()
            await stream.text('late')
            await stream.close()
        self.assertEqual(len(slack.payloads('chat.stopStream')), 2)
        self.assertEqual(
            slack.payloads('chat.postMessage')[0]['markdown_text'], 'late'
        )

    async def test_stream_stopped_by_slack(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            slack.responses['chat.stopStream'] = [
                {'ok': False, 'error': 'message_not_in_streaming_state'},
            ]
            stream = render.Stream(slack.client, 'C1', '1.0')
            await stream.text('x' * 11_000)
            await stream.text('y' * 2_000)
            await stream.close()
            await stream.close()
        self.assertEqual(len(slack.payloads('chat.stopStream')), 1)
        self.assertEqual(
            slack.payloads('chat.postMessage')[0]['markdown_text'], 'y' * 2_000
        )

    async def test_buffered_text_kept_when_slack_stopped_stream(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            slack.responses['chat.stopStream'] = [
                {'ok': False, 'error': 'message_not_in_streaming_state'},
            ]
            stream = render.Stream(slack.client, 'C1', '1.0')
            await stream.text('x' * 1_000)
            await stream.text('tail')
            await stream.close()
        self.assertEqual(
            slack.payloads('chat.postMessage')[0]['markdown_text'], 'tail'
        )


class NeutralizeBroadcastsTimeTestCase(unittest.TestCase):
    def test_many_carets_without_pipe(self) -> None:
        text = '<!date^1^' + '^' * 100_000
        started = time.monotonic()
        result = render.neutralize_broadcasts(text)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(result.startswith('<\u200b!date'))
