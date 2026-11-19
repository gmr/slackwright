import unittest

from slackwright import progress, render
from tests import fake_slack


class ReporterTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_thread_status(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            reporter = progress.Reporter(slack.client, 'C1', '1.0')
            await reporter.start('a', 'Searching docs')
            await reporter.start('b', 'Reading Jira')
            await reporter.finish('a')
            await reporter.finish('b', ok=False)
        self.assertEqual(
            [
                p['status']
                for p in slack.payloads('assistant.threads.setStatus')
            ],
            ['Searching docs…', 'Reading Jira…', 'Reading Jira…', ''],
        )

    async def test_slack_error_is_ignored(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            slack.responses['assistant.threads.setStatus'] = {
                'ok': False,
                'error': 'not_allowed',
            }
            reporter = progress.Reporter(slack.client, 'C1', '1.0')
            await reporter.start('a', 'Searching')
            await reporter.finish('a')

    async def test_stream_error_is_ignored(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            slack.responses['chat.startStream'] = {
                'ok': False,
                'error': 'not_allowed',
            }
            stream = render.Stream(slack.client, 'C1', '1.0', buffer_size=1)
            reporter = progress.Reporter(
                slack.client, 'C1', '1.0', stream=stream
            )
            await reporter.start('a', 'Searching')
            await reporter.finish('a')

    async def test_connection_error_is_ignored(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            reporter = progress.Reporter(slack.client, 'C1', '1.0')
        await reporter.start('a', 'Searching')

    async def test_stream_tasks(self) -> None:
        async with fake_slack.FakeSlack() as slack:
            stream = render.Stream(slack.client, 'C1', '1.0')
            reporter = progress.Reporter(
                slack.client, 'C1', '1.0', stream=stream
            )
            await reporter.start('a', 'Searching')
            await reporter.finish('a', ok=False, output='timeout')
            await stream.close()
        chunks = [
            chunk
            for _, payload in slack.calls
            for chunk in payload.get('chunks') or []
        ]
        self.assertEqual(
            [(c['id'], c['status']) for c in chunks],
            [('a', 'in_progress'), ('a', 'error')],
        )
        self.assertEqual(chunks[1]['output'], 'timeout')
        self.assertNotIn('assistant.threads.setStatus', slack.methods())
