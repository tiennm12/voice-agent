import json
import re
from functools import partial

from aiohttp import web

from .config import effective_settings

# The shape CallRecord gives its ids. Anything else never reaches the database or the disk.
CALL_ID = re.compile(r'\d{8}T\d{6}_[0-9a-f]{8}')
# Editable settings and their maximum length.
TEXT_SETTINGS = {'owner_name': 80, 'greeting': 500, 'system_doc': 8000}

json_response = partial(web.json_response, dumps=partial(json.dumps, ensure_ascii=False))


async def read_json(request):
    try:
        return await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text='body must be JSON')


def setup_api(app, db, config):
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
        changes = {}
        for key, value in body.items():
            if key not in TEXT_SETTINGS:
                raise web.HTTPBadRequest(text=f'unknown setting: {key}')
            if value is None or value == '':
                changes[key] = None  # back to the default
            elif not isinstance(value, str) or len(value) > TEXT_SETTINGS[key]:
                raise web.HTTPBadRequest(text=f'{key} must be text of at most {TEXT_SETTINGS[key]} characters')
            else:
                changes[key] = value.strip() or None
        db.set_settings(changes)
        return json_response(effective_settings(config, db.get_settings()))

    app.router.add_get('/api/calls', list_calls)
    app.router.add_get('/api/calls/{id}', get_call)
    app.router.add_post('/api/calls/{id}/read', mark_read)
    app.router.add_get('/api/calls/{id}/audio', get_audio)
    app.router.add_get('/api/stats', get_stats)
    app.router.add_get('/api/settings', get_settings)
    app.router.add_put('/api/settings', put_settings)
