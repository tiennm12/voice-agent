import asyncio
import json
import logging

from aiohttp import WSMsgType

log = logging.getLogger(__name__)


class JsonWsClient:
    """One upstream JSON-over-WebSocket connection at a time, with single-listener events.

    Subclasses implement _handle(event). Emits 'error' and 'disconnected' (the socket
    closed without us asking for it) on top of whatever the subclass emits.
    """

    def __init__(self, ws_connect):
        self._ws_connect = ws_connect
        self._ws = None
        # Set right before we close the socket ourselves, so the reader can tell a
        # deliberate shutdown apart from an unexpected drop.
        self._expect_close = False
        self._handlers = {}
        self._tasks = set()

    def on(self, name, handler):
        self._handlers[name] = handler

    def emit(self, name, *args):
        handler = self._handlers.get(name)
        if handler:
            handler(*args)

    async def _open(self, url, headers, hello):
        self.close()  # a previous connection, if any, is superseded
        self._expect_close = False
        ws = await self._ws_connect(url, headers=headers)
        self._ws = ws
        self._spawn(self._read(ws))
        await self._send(hello)

    def close(self):
        self._expect_close = True
        ws, self._ws = self._ws, None
        if ws is not None:
            self._spawn(ws.close())

    async def _send(self, payload):
        ws = self._ws
        if ws is None or ws.closed:
            return
        try:
            await ws.send_str(json.dumps(payload))
        except ConnectionError:
            pass  # the reader reports the drop as 'disconnected'

    async def _send_bytes(self, data):
        ws = self._ws
        if ws is None or ws.closed:
            return
        try:
            await ws.send_bytes(data)
        except ConnectionError:
            pass  # the reader reports the drop as 'disconnected'

    async def _read(self, ws):
        async for msg in ws:
            if ws is not self._ws:
                return  # superseded by a later connect() or closed by us
            if msg.type is WSMsgType.ERROR:
                self.emit('error', ws.exception() or RuntimeError('WebSocket error'))
            if msg.type is not WSMsgType.TEXT:
                continue
            try:
                event = json.loads(msg.data)
            except ValueError as err:
                self.emit('error', err)
                continue
            self._handle(event)
        # A send on a closed socket is a silent no-op, so without this the whole
        # pipeline goes dead with zero signal.
        if ws is self._ws and not self._expect_close:
            self._expect_close = True
            self.emit('disconnected')

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)  # keep a reference: the loop only holds tasks weakly
        task.add_done_callback(self._reap)

    def _reap(self, task):
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error('%s background task failed', type(self).__name__, exc_info=task.exception())
