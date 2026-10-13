import asyncio
import contextlib
import json
import logging
import uuid
from functools import partial
from pathlib import Path

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from openai import AsyncOpenAI

from .api import setup_api
from .auth import setup_auth
from .call_record import CallRecord, purge_old_calls
from .config import build_system_prompt, effective_settings, load_config
from .db import Database
from .llm_client import stream_completion
from .realtime_stt_client import RealtimeSttClient
from .session_orchestrator import SessionOrchestrator
from .tts_client import AUDIO_FORMATS, TtsClient

log = logging.getLogger(__name__)

PUBLIC_DIR = Path(__file__).resolve().parent.parent / 'public'



DB = web.AppKey('db', Database)
PURGE_INTERVAL = 24 * 3600


def create_app(config, *, ws_connect=None, openai_client=None):
    """ws_connect / openai_client default to the real upstreams; tests pass fakes."""
    upstream = {}
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.data_dir / 'voicemail.db')

    def purge():
        purged = purge_old_calls(db, config.data_dir, config.retention_days, config.tz)
        if purged:
            log.info('[server] purged %d call(s) older than %g days', len(purged), config.retention_days)

    async def purge_daily():
        while True:
            await asyncio.sleep(PURGE_INTERVAL)
            purge()

    async def lifetime(app):
        upstream['http'] = aiohttp.ClientSession()
        upstream['ws_connect'] = ws_connect or upstream['http'].ws_connect
        upstream['openai'] = openai_client or AsyncOpenAI(api_key=config.openai_api_key)
        purge()
        purging = asyncio.create_task(purge_daily())
        yield
        purging.cancel()
        await upstream['http'].close()
        await upstream['openai'].close()
        db.close()

    async def index(request):
        return web.FileResponse(PUBLIC_DIR / 'index.html')

    async def call(request):
        browser_ws = web.WebSocketResponse()
        await browser_ws.prepare(request)
        # Short id to correlate one call's log lines in a multi-call server log.
        session_id = uuid.uuid4().hex[:8]
        record = CallRecord(
            session_id=session_id, data_dir=config.data_dir, pricing=config.pricing, db=db, tz=config.tz,
        )

        # One queue + one sender per call keeps control messages and audio in the
        # order the orchestrator produced them. None means: the agent hangs up.
        outbox = asyncio.Queue()

        async def send_outbox():
            with contextlib.suppress(ConnectionError):  # browser gone; the receive loop ends the call
                while (item := await outbox.get()) is not None:
                    if isinstance(item, bytes):
                        await browser_ws.send_bytes(item)
                    else:
                        await browser_ws.send_str(json.dumps(item, ensure_ascii=False))
                await browser_ws.close()

        def send_control(message):
            if message['type'] == 'transcript':
                record.add_transcript(message['role'], message['text'])
            outbox.put_nowait(message)

        stt = RealtimeSttClient(
            api_key=config.openai_api_key,
            model=config.openai_realtime_model,
            transcribe_model=config.openai_transcribe_model,
            ws_connect=upstream['ws_connect'],
        )
        stt.on('usage', record.add_stt_usage)
        # The bridge page asks for 'pcm'; anything unrecognized gets the mic page's MP3.
        audio = request.query.get('audio')
        if audio not in AUDIO_FORMATS:
            audio = 'mp3'
        tts = TtsClient(
            api_key=config.elevenlabs_api_key,
            voice_id=config.elevenlabs_voice_id,
            model_id=config.elevenlabs_model_id,
            ws_connect=upstream['ws_connect'],
            audio=audio,
        )
        # Read per call, so what the owner saves in the dashboard applies from the next call on.
        settings = effective_settings(config, db.get_settings())
        orchestrator = SessionOrchestrator(
            system_prompt=build_system_prompt(settings['system_doc'], settings['owner_name']),
            stt_client=stt,
            llm_stream=partial(stream_completion, upstream['openai'], on_usage=record.add_llm_usage),
            tts_client=tts,
            session_id=session_id,
            greeting=settings['greeting'],
            send_control=send_control,
            send_audio=outbox.put_nowait,
            on_hangup=lambda: outbox.put_nowait(None),
            silence_timeout=config.silence_timeout,
            max_call=config.max_call,
        )

        log.info('[%s] [server] new call connected', session_id)
        sender = asyncio.create_task(send_outbox())
        try:
            try:
                await orchestrator.start()
            except Exception:
                log.exception('[%s] [server] failed to start session', session_id)
                with contextlib.suppress(ConnectionError):
                    await browser_ws.send_str(json.dumps(
                        {'type': 'error', 'message': 'Không thể kết nối tới dịch vụ giọng nói.'}, ensure_ascii=False,
                    ))
            else:
                async for msg in browser_ws:
                    if msg.type is aiohttp.WSMsgType.BINARY:
                        record.write_audio(msg.data)
                        await orchestrator.handle_audio_chunk(msg.data)
                    elif msg.type is aiohttp.WSMsgType.TEXT:
                        record.set_caller(msg.data)
        finally:
            log.info('[%s] [server] call ended', session_id)
            orchestrator.stop()
            sender.cancel()
            record.usage['tts_characters'] = tts.characters_sent
            record.close()  # the call is on disk before anything below can fail

        await browser_ws.close()
        await record.summarize_and_notify(
            openai_client=upstream['openai'],
            http=upstream['http'],
            telegram_bot_token=config.telegram_bot_token,
            telegram_chat_id=config.telegram_chat_id,
        )
        return browser_ws

    async def forbid_stale_copies(request, response):
        # With no Cache-Control a browser caches pages and scripts on a guess, and keeps
        # showing them after a deploy or a logout. API answers hold call data: never stored.
        response.headers.setdefault('Cache-Control', 'no-store' if request.path.startswith('/api/') else 'no-cache')

    app = web.Application()
    app[DB] = db
    app.cleanup_ctx.append(lifetime)
    app.on_response_prepare.append(forbid_stale_copies)
    setup_auth(app, config.dashboard_password, PUBLIC_DIR / 'login.html')
    setup_api(app, db, config)
    app.router.add_get('/', index)
    app.router.add_get('/ws', call)
    app.router.add_static('/', PUBLIC_DIR)
    return app


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = load_config()
    web.run_app(create_app(config), port=config.port)


if __name__ == '__main__':
    main()
