import asyncio
import json
from types import SimpleNamespace

from aiohttp import WSMessage, WSMsgType


async def tick(times=5):
    """Let already-scheduled tasks and callbacks run."""
    for _ in range(times):
        await asyncio.sleep(0)


class FakeWs:
    """Stands in for an aiohttp client websocket: records sends, replays fed messages."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self._incoming = asyncio.Queue()

    async def send_str(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.closed = True
        self._incoming.put_nowait(None)

    def feed(self, payload):
        self._incoming.put_nowait(WSMessage(WSMsgType.TEXT, json.dumps(payload), None))

    def drop(self):
        """The remote end closes the socket."""
        self._incoming.put_nowait(None)

    def exception(self):
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self._incoming.get()
        if msg is None:
            raise StopAsyncIteration
        return msg


def fake_ws_connect(*sockets):
    """Returns (ws_connect, calls): each call hands out the next socket and records (url, headers)."""
    remaining = list(sockets)
    calls = []

    async def ws_connect(url, headers=None):
        calls.append((url, headers))
        return remaining.pop(0)

    return ws_connect, calls


class FakeOpenai:
    """chat.completions.create(): streams `reply` when stream=True, else returns `summary` as JSON."""

    def __init__(self, reply='Xin chào.', summary=None, summary_error=None):
        self.reply = reply
        self.summary = summary or {'caller_name': 'Anh Nam', 'summary': 'Nhờ gọi lại về hợp đồng.', 'urgency': 'cao'}
        self.summary_error = summary_error
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **params):
        self.calls.append(params)
        if params.get('stream'):
            return self._stream()
        if self.summary_error:
            raise self.summary_error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.summary)))],
            usage=SimpleNamespace(prompt_tokens=200, completion_tokens=40),
        )

    async def _stream(self):
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=self.reply))])
        yield SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10))

    async def close(self):
        pass


class FakeHttp:
    """Stands in for aiohttp.ClientSession.post(); records (url, json) per request."""

    def __init__(self, status=200, error=None):
        self.status = status
        self.error = error
        self.posts = []

    def post(self, url, json=None, **kwargs):
        self.posts.append((url, json))
        if self.error:
            raise self.error
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return 'telegram says no'
