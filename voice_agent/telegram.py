import asyncio
import logging
import time
from datetime import datetime

import aiohttp

log = logging.getLogger(__name__)

MAX_TEXT = 4096  # Telegram's limit for one message
# Seconds to wait before each retry of a message Telegram could not take right now (outage,
# rate limit, network drop). The notification is the point of the product: worth a few tries.
RETRY_DELAYS = (2, 10)


def notification_text(call):
    """The missed-call message, from a call as the database returns it."""
    started = datetime.fromisoformat(call['started_at'])
    minutes, seconds = divmod(call.get('duration_seconds') or 0, 60)
    cost = call.get('cost') or {}
    price = f"${cost.get('total_usd', 0):.4f}"
    if cost.get('total_vnd'):
        price += f" (~{cost['total_vnd']:,}đ)".replace(',', '.')
    return '\n'.join([
        f"Cuộc gọi nhỡ từ {call.get('caller') or 'số không rõ'} lúc {started:%H:%M %d/%m/%Y}",
        f"Người gọi: {call.get('caller_name') or 'Không rõ'}",
        f"Nội dung: {call.get('summary') or ''}",
        f"Độ khẩn: {call.get('urgency') or 'không rõ'}",
        f"Thời lượng: {minutes}:{seconds:02d} · Chi phí: {price}",
    ])


async def send_message(http, token, chat_id, text, button=None):
    """Sends one message, optionally with a (label, url) button under it. Returns whether Telegram took it."""
    payload = {'chat_id': chat_id, 'text': text[:MAX_TEXT]}
    if button:
        label, url = button
        payload['reply_markup'] = {'inline_keyboard': [[{'text': label, 'url': url}]]}
    for delay in (*RETRY_DELAYS, None):
        try:
            async with http.post(
                f'https://api.telegram.org/bot{token}/sendMessage', json=payload, timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 200:
                    return True
                # Never log the URL: it carries the bot token.
                log.error('telegram sendMessage failed: %s %s', response.status, (await response.text())[:300])
                if response.status < 500 and response.status != 429:
                    return False  # refused (bad token, unknown chat): a retry gets the same answer
        except Exception as err:
            # No traceback, and the token scrubbed: client errors like to quote the request URL.
            log.error('telegram sendMessage failed: %s %s', type(err).__name__, str(err).replace(token, '***'))
        if delay is not None:
            await asyncio.sleep(delay)
    return False


class TelegramNotifier:
    """Decides what the owner hears about a finished call, and nags about urgent ones until they are read."""

    def __init__(self, *, http, token, chat_id, db, link_for=None, reminder_minutes=5, max_reminders=3):
        self._http = http
        self._token = token
        self._chat_id = chat_id
        self._db = db
        self._link_for = link_for  # call id -> URL the owner can open without logging in, or None
        self._reminder_minutes = reminder_minutes
        self._max_reminders = max_reminders
        self._reminders = set()

    async def notify_call(self, call):
        if call.get('category') == 'spam':
            log.info('[%s] spam: not notifying', call['session_id'])
            return
        text = notification_text(call)
        link = self._link_for(call['id']) if self._link_for else None
        button = ('Nghe ghi âm', link) if link else None
        urgent = call.get('urgency') == 'cao'
        await self._send(f'🔴 KHẨN CẤP\n{text}' if urgent else text, button)
        if urgent:
            # ponytail: reminders live in memory and die with the process. Persist a
            # next-reminder time on the call if they must survive a restart.
            task = asyncio.create_task(self._remind(call['id'], text, button))
            self._reminders.add(task)
            task.add_done_callback(self._reminders.discard)

    async def _remind(self, call_id, text, button):
        for attempt in range(1, self._max_reminders + 1):
            await asyncio.sleep(self._reminder_minutes * 60)
            call = self._db.get_call(call_id)
            if call is None or call['read']:
                return
            await self._send(
                f'🔴 Nhắc lại lần {attempt}/{self._max_reminders}: cuộc gọi khẩn chưa xem.\n{text}', button,
            )

    def _send(self, text, button):
        return send_message(self._http, self._token, self._chat_id, text, button)

    def alert(self, text):
        """A message about the system itself, sent in the background (see TelegramAlerts)."""
        task = asyncio.create_task(self._send(f'⚠️ Voice Agent: {text}', None))
        self._reminders.add(task)
        task.add_done_callback(self._reminders.discard)

    def close(self):
        for task in self._reminders:
            task.cancel()


class TelegramAlerts(logging.Handler):
    """Forwards the server's ERROR log lines to Telegram, at most one message per `interval` seconds.

    An expired key or an exhausted credit fails every call while the server keeps running: the owner
    hears of it before a caller has to complain. Errors in a burst are counted into the next message.
    """

    def __init__(self, notifier, interval=600):
        super().__init__(logging.ERROR)
        self._notifier = notifier
        self._interval = interval
        self._last = None
        self._held = 0

    def emit(self, record):
        if record.name == __name__:
            return  # Telegram's own failures can't be reported through Telegram
        now = time.monotonic()
        if self._last is not None and now - self._last < self._interval:
            self._held += 1
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # logged outside the server's event loop: nothing can send it
        text = record.getMessage()
        if record.exc_info and record.exc_info[1]:
            text += f' ({type(record.exc_info[1]).__name__}: {record.exc_info[1]})'
        if self._held:
            text += f' (+{self._held} lỗi trước đó)'
        self._last, self._held = now, 0
        self._notifier.alert(text[:500])
