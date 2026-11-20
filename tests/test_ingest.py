import base64
import unittest

import httpx

from slackwright import ingest
from tests import fake_slack

PNG = b'\x89PNG\r\n\x1a\nfake'


class FileServer:
    """An httpx transport that serves files and records requests."""

    def __init__(
        self,
        files: dict[str, tuple[int, str, bytes]],
        redirects: dict[str, str] | None = None,
    ) -> None:
        self.files = files
        self.redirects = redirects or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, mimetype, body = self.files.get(
            str(request.url), (404, 'text/plain', b'')
        )
        headers = {'content-type': mimetype}
        if location := self.redirects.get(str(request.url)):
            headers['location'] = location
        return httpx.Response(status, headers=headers, content=body)

    def client(self) -> httpx.AsyncClient:
        return ingest.http_client(
            'xoxb-test', transport=httpx.MockTransport(self)
        )


class IsSlackFileURLTestCase(unittest.TestCase):
    def test_urls(self) -> None:
        cases = {
            'https://files.slack.com/files-pri/T1-F1/a.png': True,
            'https://FILES.slack.com/a': True,
            'https://files.slack.com:443/a': True,
            'http://files.slack.com/a': False,
            'https://files.slack.com:8443/a': False,
            'https://files.slack.com.evil.example/a': False,
            'https://evil.example/files.slack.com': False,
            'https://user@files.slack.com/a': False,
            'https://evil.example\\@files.slack.com/a': False,
            'https://files.slack.com/a b': False,
            '': False,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertIs(ingest.is_slack_file_url(url), expected)


class FileToBlockTestCase(unittest.TestCase):
    def test_types(self) -> None:
        self.assertEqual(
            ingest.file_to_block('application/pdf', b'%PDF')['type'],
            'document',
        )
        image = ingest.file_to_block('image/png; charset=x', PNG)
        self.assertEqual(image['source']['media_type'], 'image/png')
        self.assertEqual(base64.b64decode(image['source']['data']), PNG)
        self.assertEqual(
            ingest.file_to_block('application/json', b'{"a":1}'),
            {'type': 'text', 'text': '{"a":1}'},
        )
        self.assertEqual(
            ingest.file_to_block('application/octet-stream', b'plain'),
            {'type': 'text', 'text': 'plain'},
        )
        self.assertIn(
            'could not be decoded',
            ingest.file_to_block('application/octet-stream', b'\xff\xfe')[
                'text'
            ],
        )
        self.assertIn(
            'unsupported file type application/zip',
            ingest.file_to_block('application/zip', b'PK')['text'],
        )

    def test_image_over_limit(self) -> None:
        content = b'x' * (ingest.MAX_IMAGE_BYTES + 1)
        block = ingest.file_to_block('image/png', content)
        self.assertEqual(block['type'], 'text')
        self.assertIn('image is larger than the maximum', block['text'])


class DownloadTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_download(self) -> None:
        url = 'https://files.slack.com/a.png'
        server = FileServer({url: (200, 'image/png', PNG)})
        async with server.client() as client:
            block = await ingest.download(client, url)
        assert block is not None
        self.assertEqual(block['type'], 'image')
        self.assertEqual(
            server.requests[0].headers['authorization'], 'Bearer xoxb-test'
        )

    async def test_token_never_goes_to_other_hosts(self) -> None:
        server = FileServer({})
        async with server.client() as client:
            block = await ingest.download(client, 'https://evil.example/a')
        self.assertIsNone(block)
        self.assertEqual(server.requests, [])

    async def test_redirects_are_not_followed(self) -> None:
        url = 'https://files.slack.com/a'
        for target in ('https://evil.example/b', 'https://files.slack.com/b'):
            with self.subTest(target=target):
                server = FileServer(
                    {
                        url: (302, 'text/plain', b''),
                        target: (200, 'text/plain', b'x'),
                    },
                    redirects={url: target},
                )
                async with server.client() as client:
                    self.assertIsNone(await ingest.download(client, url))
                self.assertEqual(
                    [str(request.url) for request in server.requests], [url]
                )

    async def test_invalid_url(self) -> None:
        server = FileServer({})
        async with server.client() as client:
            self.assertIsNone(await ingest.download(client, 'https://[x/a'))
        self.assertEqual(server.requests, [])

    async def test_oversized_file(self) -> None:
        url = 'https://files.slack.com/big.txt'
        server = FileServer({url: (200, 'text/plain', b'x' * 2048)})
        async with server.client() as client:
            block = await ingest.download(client, url, max_bytes=1024)
        assert block is not None
        self.assertIn('larger than the maximum', block['text'])

    async def test_file_blocks_skips_failures(self) -> None:
        url = 'https://files.slack.com/a.txt'
        server = FileServer({url: (200, 'text/plain', b'hello')})
        files = [
            {'url_private': url},
            {'url_private_download': 'https://files.slack.com/missing'},
            {'name': 'no url'},
        ]
        async with server.client() as client:
            blocks = await ingest.file_blocks(client, files)
        self.assertEqual(blocks, [{'type': 'text', 'text': 'hello'}])


class ThreadTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_thread(self) -> None:
        image_url = 'https://files.slack.com/chart.png'
        server = FileServer({image_url: (200, 'image/png', PNG)})
        async with fake_slack.FakeSlack() as slack:
            users = {
                'UBOT': {'name': 'aj'},
                'U1': {'profile': {'display_name': 'ann'}},
                'U2': {'real_name': 'Bob B'},
            }
            slack.responses['users.info'] = lambda payload: {
                'ok': True,
                'user': users[payload['user']],
            }
            slack.responses['conversations.replies'] = [
                {
                    'ok': True,
                    'messages': [
                        {'user': 'UBOT', 'text': 'hello', 'ts': '1'},
                        {'user': 'U1', 'text': 'hi <@UBOT>', 'ts': '2'},
                        {
                            'user': 'U2',
                            'text': 'see <@U1|ann>',
                            'ts': '3',
                            'files': [{'url_private': image_url}],
                        },
                    ],
                    'response_metadata': {'next_cursor': 'next'},
                },
                {
                    'ok': True,
                    'messages': [
                        {'subtype': 'channel_join', 'user': 'U3'},
                        {
                            'user': 'UBOT',
                            'text': 'here',
                            'ts': '4',
                            'files': [{'name': 'report.csv'}],
                        },
                        {'user': 'U1', 'text': '', 'ts': '5'},
                    ],
                },
            ]
            async with server.client() as client:
                messages = await ingest.thread(
                    slack.client, client, 'C1', '1', 'UBOT'
                )
        pages = slack.payloads('conversations.replies')
        self.assertNotIn('cursor', pages[0])
        self.assertEqual(pages[1]['cursor'], 'next')
        self.assertEqual(len(slack.payloads('users.info')), 3)
        self.assertEqual(len(messages), 2)
        user, assistant = messages
        self.assertEqual(user['role'], 'user')
        self.assertEqual(
            [part['type'] for part in user['content']],
            ['text', 'text', 'image'],
        )
        self.assertEqual(user['content'][0]['text'], '<@ann> [ts=2] hi @aj')
        self.assertEqual(
            user['content'][1]['text'], '<@Bob B> [ts=3] see @ann'
        )
        self.assertEqual(
            assistant,
            {'role': 'assistant', 'content': 'here\n\n[Attached: report.csv]'},
        )

    async def _thread(
        self, replies: list[dict[str, str]], max_messages: int
    ) -> tuple[list[ingest.Message], fake_slack.FakeSlack]:
        async with fake_slack.FakeSlack() as slack:
            slack.responses['users.info'] = {'ok': True, 'user': {}}
            slack.responses['conversations.replies'] = {
                'ok': True,
                'messages': replies,
            }
            async with FileServer({}).client() as client:
                messages = await ingest.thread(
                    slack.client, client, 'C1', '1', 'UBOT', max_messages
                )
        return messages, slack

    async def test_max_messages(self) -> None:
        replies = [
            {'user': 'U1', 'text': 'one', 'ts': '1'},
            {'user': 'UBOT', 'text': 'two', 'ts': '2'},
            {'user': 'U1', 'text': 'three', 'ts': '3'},
            {'user': 'UBOT', 'text': 'four', 'ts': '4'},
        ]
        expected = [
            {'role': 'user', 'content': '<@U1> [ts=3] three'},
            {'role': 'assistant', 'content': 'four'},
        ]
        messages, _ = await self._thread(replies, 2)
        self.assertEqual(messages, expected)
        # The window starts with an assistant turn, which is removed.
        messages, _ = await self._thread(replies, 3)
        self.assertEqual(messages, expected)
        messages, slack = await self._thread(replies, 0)
        self.assertEqual(messages, [])
        self.assertEqual(slack.calls, [])

    async def test_thread_broadcast_is_kept(self) -> None:
        replies = [
            {'user': 'U1', 'text': 'hi', 'ts': '1'},
            {
                'user': 'U1',
                'text': 'also',
                'ts': '2',
                'subtype': 'thread_broadcast',
            },
        ]
        messages, _ = await self._thread(replies, 50)
        self.assertEqual(
            messages[0]['content'],
            '<@U1> [ts=1] hi\n\n<@U1> [ts=2] also',
        )

    async def test_file_share_is_kept(self) -> None:
        replies = [
            {'user': 'U1', 'text': 'hi', 'ts': '1'},
            {
                'user': 'U1',
                'text': 'see this',
                'ts': '2',
                'subtype': 'file_share',
            },
        ]
        messages, _ = await self._thread(replies, 50)
        self.assertEqual(
            messages[0]['content'],
            '<@U1> [ts=1] hi\n\n<@U1> [ts=2] see this',
        )
