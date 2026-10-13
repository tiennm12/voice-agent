import json
import re
import secrets
from datetime import datetime
from functools import partial

from aiohttp import web

from .call_record import SPAM
from .config import effective_settings

# The shape CallRecord gives its ids. Anything else never reaches the database or the disk.
CALL_ID = re.compile(r'\d{8}T\d{6}_[0-9a-f]{8}')
# Editable text settings and their maximum length.
TEXT_SETTINGS = {'owner_name': 80, 'greeting': 500, 'system_doc': 8000}
MAX_BLOCKED_NUMBERS = 500
MAX_NUMBER_DIGITS = 20

json_response = partial(web.json_response, dumps=partial(json.dumps, ensure_ascii=False))


async def read_json(request):
    try:
        return await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text='body must be JSON')


def normalize_number(text):
    """One spelling per phone number, so '+84 901 234 567' and '0901234567' compare equal.

    ponytail: knows Vietnam's +84 prefix only. Use a phone-number library if callers ever come from elsewhere.
    """
    digits = re.sub(r'\D', '', text)
    if digits.startswith('84') and len(digits) >= 11:
        digits = '0' + digits[2:]
    return digits


def clean_blocked_numbers(value):
    if not isinstance(value, list) or len(value) > MAX_BLOCKED_NUMBERS:
        raise web.HTTPBadRequest(text=f'blocked_numbers must be a list of at most {MAX_BLOCKED_NUMBERS} numbers')
    numbers = []
    for entry in value:
        if not isinstance(entry, str):
            raise web.HTTPBadRequest(text='blocked_numbers must contain text only')
        number = normalize_number(entry)
        if len(number) > MAX_NUMBER_DIGITS:
            raise web.HTTPBadRequest(text=f'not a phone number: {entry[:40]}')
        if number and number not in numbers:
            numbers.append(number)
    return numbers or None


def clean_setting(key, value):
    """A setting as it will be stored; None means back to the default. Raises 400 on anything unexpected."""
    if value is None or value == '':
        if key in TEXT_SETTINGS or key == 'blocked_numbers':
            return None
    elif key == 'blocked_numbers':
        return clean_blocked_numbers(value)
    elif key in TEXT_SETTINGS:
        if not isinstance(value, str) or len(value) > TEXT_SETTINGS[key]:
            raise web.HTTPBadRequest(text=f'{key} must be text of at most {TEXT_SETTINGS[key]} characters')
        return value.strip() or None
    raise web.HTTPBadRequest(text=f'unknown setting: {key}')


def setup_api(app, db, config, public_dir):
    """The dashboard's JSON API. Every route sits behind the session middleware."""

    def call_or_404(request):
        call_id = request.match_info['id']
        call = db.get_call(call_id) if CALL_ID.fullmatch(call_id) else None
        if call is None:
            raise web.HTTPNotFound()
        return call

    async def list_calls(request):
        try:
            limit = int(request.query.get('limit', 30))
            if not 1 <= limit <= 100:
                raise ValueError(limit)
            calls = db.list_calls(before=request.query.get('before'), limit=limit, only=request.query.get('filter'))
        except ValueError:
            raise web.HTTPBadRequest(text='bad limit or filter')
        return json_response({'calls': calls})

    async def get_call(request):
        return json_response(call_or_404(request))

    async def mark_read(request):
        call = call_or_404(request)
        body = await read_json(request)
        if not isinstance(body, dict) or not isinstance(body.get('read'), bool):
            raise web.HTTPBadRequest(text='"read" must be true or false')
        db.mark_read(call['id'], body['read'])
        return json_response({'read': body['read']})

    async def get_audio(request):
        call = call_or_404(request)
        recording = config.data_dir / 'calls' / call['id'] / 'caller.wav'
        if not recording.is_file():
            raise web.HTTPNotFound()
        # FileResponse handles Range, so the player can seek. The type is spelled out because
        # the guessed one is 'audio/vnd.wave', which not every player recognizes.
        return web.FileResponse(recording, headers={'Content-Type': 'audio/wav'})

    async def get_stats(request):
        by = request.query.get('by', 'day')
        try:
            return json_response({'by': by, 'rows': db.stats(by)})
        except ValueError:
            raise web.HTTPBadRequest(text='by must be day, week or month')

    async def get_settings(request):
        return json_response(effective_settings(config, db.get_settings()))

    async def put_settings(request):
        body = await read_json(request)
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='body must be an object')
        db.set_settings({key: clean_setting(key, value) for key, value in body.items()})
        return json_response(effective_settings(config, db.get_settings()))

    async def check_blocked(request):
        """Asked by the bridge page before it answers. A blocked caller is logged, never picked up."""
        body = await read_json(request)
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='body must be an object')
        caller = normalize_number(str(body.get('caller') or ''))[:MAX_NUMBER_DIGITS]
        if not caller or caller not in effective_settings(config, db.get_settings())['blocked_numbers']:
            return json_response({'blocked': False})
        now = datetime.now(config.tz)
        session_id = secrets.token_hex(4)
        call_id = f'{now:%Y%m%dT%H%M%S}_{session_id}'
        db.save_call({
            'id': call_id, 'session_id': session_id, 'caller': caller,
            'started_at': now.isoformat(), 'ended_at': now.isoformat(), 'duration_seconds': 0,
            'summary': 'Số nằm trong danh sách chặn: không nhấc máy.', 'urgency': 'thấp', 'category': SPAM,
        })
        db.mark_read(call_id)  # nothing to read: it must not show up as an unread call
        return json_response({'blocked': True})

    async def listen_page(request):
        # One call on a page of its own, for call links; the page fetches the call itself.
        return web.FileResponse(public_dir / 'listen.html')

    app.router.add_get('/c/{id}', listen_page)
    app.router.add_get('/api/calls', list_calls)
    app.router.add_get('/api/calls/{id}', get_call)
    app.router.add_post('/api/calls/{id}/read', mark_read)
    app.router.add_get('/api/calls/{id}/audio', get_audio)
    app.router.add_get('/api/stats', get_stats)
    app.router.add_get('/api/settings', get_settings)
    app.router.add_put('/api/settings', put_settings)
    app.router.add_post('/api/blocked', check_blocked)
