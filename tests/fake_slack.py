"""A fake Slack Web API server for tests.

Tests point a real ``AsyncWebClient`` at it, so no ``slack_sdk`` code
is mocked. The server records each call and returns a canned response.

"""

from __future__ import annotations

import typing

from aiohttp import test_utils, web
from slack_sdk.web import async_client


class FakeSlack:
    """Use as ``async with FakeSlack() as slack:``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, typing.Any]]] = []
        self.responses: dict[str, typing.Any] = {}
        self.uploads: list[bytes] = []
        self._ts = 0
        self._server: test_utils.TestServer | None = None
        self.client: async_client.AsyncWebClient

    async def __aenter__(self) -> FakeSlack:
        app = web.Application()
        app.router.add_route('*', '/api/{method}', self._api)
        app.router.add_post('/upload', self._upload)
        self._server = test_utils.TestServer(app)
        await self._server.start_server()
        self.client = async_client.AsyncWebClient(
            token='xoxb-test', base_url=str(self._server.make_url('/api/'))
        )
        self.responses['files.getUploadURLExternal'] = {
            'ok': True,
            'upload_url': str(self._server.make_url('/upload')),
            'file_id': 'F0001',
        }
        self.responses['files.completeUploadExternal'] = {
            'ok': True,
            'files': [{'id': 'F0001'}],
        }
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self._server is not None
        await self._server.close()

    def methods(self) -> list[str]:
        """Return the called API methods, in order."""
        return [method for method, _ in self.calls]

    def payloads(self, method: str) -> list[dict[str, typing.Any]]:
        """Return the payloads of all calls to ``method``."""
        return [payload for name, payload in self.calls if name == method]

    async def _api(self, request: web.Request) -> web.Response:
        method = request.match_info['method']
        payload: dict[str, typing.Any] = dict(request.query)
        if request.content_type == 'application/json':
            payload.update(await request.json())
        elif request.can_read_body:
            payload.update(await request.post())
        self.calls.append((method, payload))
        response = self.responses.get(method)
        if isinstance(response, list):
            response = response.pop(0)
        elif callable(response):
            response = response(payload)
        if response is None:
            self._ts += 1
            response = {'ok': True, 'ts': f'1700000000.{self._ts:06d}'}
        return web.json_response(response)

    async def _upload(self, request: web.Request) -> web.Response:
        self.uploads.append(await request.read())
        return web.Response(text='OK')
