import asyncio
import io
import json
import logging
import re
import secrets
from datetime import datetime
from functools import partial

import aiohttp
from aiohttp import web

from .call_record import SPAM
from .settings import MAX_NUMBER_DIGITS, clean_setting, effective_settings, normalize_number

log = logging.getLogger(__name__)

# The shape CallRecord gives its ids. Anything else never reaches the database or the disk.
CALL_ID = re.compile(r'\d{8}T\d{6}_[0-9a-f]{8}')
MAX_CLONE_BYTES = 10 * 1024 * 1024  # all voice samples of one upload together
# Each side of a call and the files it may be kept in (see CallRecord).
RECORDINGS = {'caller': ('caller.wav',), 'agent': ('agent.wav', 'agent.mp3')}
RECORDING_TYPES = {'.wav': 'audio/wav', '.mp3': 'audio/mpeg'}
MAX_VOICE_NAME = 60

json_response = partial(web.json_response, dumps=partial(json.dumps, ensure_ascii=False))


async def read_json(request):
    try:
        return await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text='body must be JSON')


def refusal(status, body):
    """ElevenLabs' own words for why a call failed (e.g. a key without the needed permission), for the owner to read."""
    detail = body.get('detail') if isinstance(body, dict) else None
    reason = detail.get('message') if isinstance(detail, dict) else detail
    return str(reason or status or 'không kết nối được')[:300]


def _has_sound(path):
    """A recording with something in it: more than the 44-byte header a WAV file starts with."""
    try:
        return path.stat().st_size > 44
    except OSError:
        return False


def setup_api(app, db, config, public_dir, upstream):
    """The dashboard's JSON API. Every route sits behind the session middleware.

    `upstream` is the server's dict of shared clients; its 'http' session exists once the app has started.
    """

    def current_settings():
        return effective_settings(config, db.get_settings())

    def call_or_404(request):
        call_id = request.match_info['id']
        call = db.get_call(call_id) if CALL_ID.fullmatch(call_id) else None
        if call is None:
            raise web.HTTPNotFound()
        return call

    async def elevenlabs(method, path, **kwargs):
        """One ElevenLabs REST call: (status, parsed body), or (None, None) when it could not be reached."""
        send = upstream['http'].get if method == 'GET' else upstream['http'].post
        try:
            async with send(
                f'https://api.elevenlabs.io{path}', headers={'xi-api-key': config.elevenlabs_api_key}, **kwargs,
            ) as response:
                return response.status, await response.json(content_type=None)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError) as err:
            log.error('elevenlabs %s %s failed: %s', method, path, type(err).__name__)
            return None, None

    # --- calls -------------------------------------------------------------

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
        call = call_or_404(request)
        # Which players the page should show: a blocked attempt, or a call cut off at once, has no recording.
        folder = config.data_dir / 'calls' / call['id']
        call['recordings'] = [
            who for who, names in RECORDINGS.items() if any(_has_sound(folder / name) for name in names)
        ]
        return json_response(call)

    async def mark_read(request):
        call = call_or_404(request)
        body = await read_json(request)
        if not isinstance(body, dict) or not isinstance(body.get('read'), bool):
            raise web.HTTPBadRequest(text='"read" must be true or false')
        db.mark_read(call['id'], body['read'])
        return json_response({'read': body['read']})

    async def get_audio(request):
        call = call_or_404(request)
        who = 'agent' if request.path.endswith('/agent-audio') else 'caller'
        for name in RECORDINGS[who]:
            recording = config.data_dir / 'calls' / call['id'] / name
            if recording.is_file():
                # FileResponse handles Range, so the player can seek. The type is spelled out because
                # the guessed one for WAV is 'audio/vnd.wave', which not every player recognizes.
                return web.FileResponse(recording, headers={'Content-Type': RECORDING_TYPES[recording.suffix]})
        raise web.HTTPNotFound()

    async def listen_page(request):
        # One call on a page of its own, for call links; the page fetches the call itself.
        return web.FileResponse(public_dir / 'listen.html')

    async def get_stats(request):
        by = request.query.get('by', 'day')
        try:
            return json_response({'by': by, 'rows': db.stats(by)})
        except ValueError:
            raise web.HTTPBadRequest(text='by must be day, week or month')

    # --- settings ----------------------------------------------------------

    async def get_settings(request):
        return json_response(current_settings())

    async def call_defaults(request):
        """What the test page's "server default" choices resolve to, so it can tell which options matter."""
        return json_response({key: getattr(config, key) for key in ('pipeline', 'stt_provider', 'vad_type')})

    async def put_settings(request):
        body = await read_json(request)
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='body must be an object')
        try:
            changes = {key: clean_setting(key, value) for key, value in body.items()}
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err))
        db.set_settings(changes)
        return json_response(current_settings())

    async def check_blocked(request):
        """Asked by the bridge page before it answers. A blocked caller is logged, never picked up."""
        body = await read_json(request)
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='body must be an object')
        caller = normalize_number(str(body.get('caller') or ''))[:MAX_NUMBER_DIGITS]
        if not caller or caller not in current_settings()['blocked_numbers']:
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

    # --- voices ------------------------------------------------------------

    async def list_voices(request):
        # ponytail: the first 100 voices only (one page). Follow next_page_token if an account ever has more.
        status, body = await elevenlabs('GET', '/v2/voices', params={'page_size': '100'}, timeout=aiohttp.ClientTimeout(total=10))
        if status != 200:
            raise web.HTTPBadGateway(text=f'ElevenLabs không trả danh sách giọng: {refusal(status, body)}')
        voices = [
            {
                'voice_id': voice['voice_id'],
                'name': voice.get('name') or voice['voice_id'],
                'category': voice.get('category'),
                'labels': voice.get('labels') or {},
                'preview_url': voice.get('preview_url'),
            }
            for voice in body.get('voices', [])
        ]
        return json_response({'current': current_settings()['voice_id'], 'voices': voices})

    async def clone_voice(request):
        """Sends the owner's voice samples to ElevenLabs and makes the resulting voice the agent's."""
        if not request.content_type.startswith('multipart/'):
            raise web.HTTPBadRequest(text='upload must be multipart/form-data')
        form = aiohttp.FormData()
        name, samples, size = None, 0, 0
        async for part in await request.multipart():
            if part.name == 'name':
                name = (await part.text()).strip()[:MAX_VOICE_NAME]
            elif part.name == 'files' and part.filename:
                sample = bytearray()
                while chunk := await part.read_chunk():
                    size += len(chunk)
                    if size > MAX_CLONE_BYTES:
                        raise web.HTTPRequestEntityTooLarge(max_size=MAX_CLONE_BYTES, actual_size=size)
                    sample.extend(chunk)
                form.add_field(
                    'files', io.BytesIO(sample), filename=part.filename,
                    content_type=part.headers.get('Content-Type', 'application/octet-stream'),
                )
                samples += 1
        if not name or not samples:
            raise web.HTTPBadRequest(text='cần tên giọng và ít nhất một file mẫu')
        form.add_field('name', name)

        status, body = await elevenlabs('POST', '/v1/voices/add', data=form, timeout=aiohttp.ClientTimeout(total=120))
        if status != 200 or not isinstance(body, dict) or not body.get('voice_id'):
            raise web.HTTPBadGateway(text=f'ElevenLabs từ chối tạo giọng: {refusal(status, body)}')
        db.set_settings({'voice_id': clean_setting('voice_id', body['voice_id'])})
        return json_response({'voice_id': body['voice_id']})

    app.router.add_get('/c/{id}', listen_page)
    app.router.add_get('/api/calls', list_calls)
    app.router.add_get('/api/calls/{id}', get_call)
    app.router.add_post('/api/calls/{id}/read', mark_read)
    app.router.add_get('/api/calls/{id}/audio', get_audio)
    app.router.add_get('/api/calls/{id}/agent-audio', get_audio)
    app.router.add_get('/api/stats', get_stats)
    app.router.add_get('/api/settings', get_settings)
    app.router.add_put('/api/settings', put_settings)
    app.router.add_get('/api/call-defaults', call_defaults)
    app.router.add_post('/api/blocked', check_blocked)
    app.router.add_get('/api/voices', list_voices)
    app.router.add_post('/api/voices/clone', clone_voice)
