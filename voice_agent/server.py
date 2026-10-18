import asyncio
import contextlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from openai import AsyncOpenAI

from .api import setup_api
from .auth import load_secret, make_call_link, setup_auth
from .call_record import CallRecord, purge_old_calls, summarize_saved_call
from .config import build_system_prompt, load_config, with_overrides
from .elevenlabs_stt_client import ElevenLabsSttClient
from .db import Database
from .live_agent import LiveAgent
from .llm_client import stream_completion
from .realtime_agent import HybridAgent, RealtimeAgent
from .realtime_stt_client import RealtimeSttClient, turn_detection
from .session_orchestrator import SessionOrchestrator
from .settings import effective_settings, pick_greeting
from .soniox_stt_client import SonioxSttClient
from .telegram import TelegramNotifier
from .tts_client import AUDIO_FORMATS, TtsClient

log = logging.getLogger(__name__)

PUBLIC_DIR = Path(__file__).resolve().parent.parent / 'public'
DB = web.AppKey('db', Database)
PURGE_INTERVAL = 24 * 3600
# Seconds. A caller sits in dead air while an upstream hangs: aiohttp would wait 5 minutes for a
# WebSocket handshake and the OpenAI client 10 minutes (with two retries) for an answer.
CONNECT_TIMEOUT = 5
OPENAI_TIMEOUT = 10
# On every response. No page may be framed (clickjacking the settings), no file sniffed into another type,
# and no address, call links' tokens included, handed on as a Referer.
# ponytail: no script-src policy yet, the pages still have inline scripts. HSTS belongs to the TLS
# front of the shared host, not to one app behind it.
SECURITY_HEADERS = {
    'Content-Security-Policy': "frame-ancestors 'none'",
    'X-Frame-Options': 'DENY',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
}


def create_app(config, *, ws_connect=None, openai_client=None, http=None):
    """ws_connect / openai_client / http default to the real upstreams; tests pass fakes."""
    upstream = {'notifier': None}
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.data_dir / 'voicemail.db')
    secret = load_secret(config.data_dir)  # signs sessions and call links

    def purge():
        purged = purge_old_calls(db, config.data_dir, config.retention_days, config.tz)
        if purged:
            log.info('[server] purged %d call(s) older than %g days', len(purged), config.retention_days)

    async def purge_daily():
        while True:
            await asyncio.sleep(PURGE_INTERVAL)
            purge()

    async def finish_cut_short_calls():
        # A restart between a call's end and its summary would leave the owner never told.
        # Only recent calls: news of a days-old call is more confusing than useful.
        since = (datetime.now(config.tz) - timedelta(days=1)).isoformat()
        for call in db.unsummarized_calls(since):
            call = await summarize_saved_call(db, upstream['openai'], config.pricing, call)
            log.info('[%s] [server] summarized a call cut short by a restart', call['session_id'])
            if upstream['notifier']:
                await upstream['notifier'].notify_call(call)

    async def lifetime(app):
        upstream['http'] = http or aiohttp.ClientSession()
        connect = ws_connect or upstream['http'].ws_connect

        async def ws_connect_in_time(url, **kwargs):
            async with asyncio.timeout(CONNECT_TIMEOUT):
                return await connect(url, **kwargs)

        upstream['ws_connect'] = ws_connect_in_time
        upstream['openai'] = openai_client or AsyncOpenAI(
            api_key=config.openai_api_key, timeout=OPENAI_TIMEOUT, max_retries=1,
        )
        if config.telegram_bot_token and config.telegram_chat_id:
            upstream['notifier'] = TelegramNotifier(
                http=upstream['http'],
                token=config.telegram_bot_token,
                chat_id=config.telegram_chat_id,
                db=db,
                # Without a public address there is nothing a phone could open: no button.
                link_for=partial(make_call_link, secret, config.public_base_url) if config.public_base_url else None,
                reminder_minutes=config.urgent_reminder_minutes,
            )
        purge()
        purging = asyncio.create_task(purge_daily())
        finishing = asyncio.create_task(finish_cut_short_calls())
        yield
        purging.cancel()
        finishing.cancel()
        if upstream['notifier']:
            upstream['notifier'].close()
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
            elif message['type'] == 'error':
                record.failed = True
            outbox.put_nowait(message)

        # The test page (call.html) may change the pipeline for this one call.
        cfg, overrides = with_overrides(config, request.query)
        # The bridge page asks for 'pcm'; anything unrecognized gets the mic page's MP3.
        audio = request.query.get('audio') if request.query.get('audio') in AUDIO_FORMATS else 'mp3'
        orchestrator, tts = build_agent(
            cfg,
            # Read per call, so what the owner saves in the dashboard applies from the next call on.
            effective_settings(config, db.get_settings()),
            record, upstream, session_id, audio,
            send_control=send_control, send_audio=outbox.put_nowait, on_hangup=lambda: outbox.put_nowait(None),
        )

        log.info('[%s] [server] new call connected%s', session_id, f' with overrides {overrides}' if overrides else '')
        sender = asyncio.create_task(send_outbox())
        try:
            try:
                await orchestrator.start()
            except Exception:
                log.exception('[%s] [server] failed to start session', session_id)
                record.failed = True
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
            if tts:
                record.usage['tts_characters'] = tts.characters_sent
            record.close()  # the call is on disk before anything below can fail

        await browser_ws.close()
        await record.summarize(upstream['openai'])
        if upstream['notifier']:
            await upstream['notifier'].notify_call(db.get_call(record.id))
        return browser_ws

    async def harden(request, response):
        # With no Cache-Control a browser caches pages and scripts on a guess, and keeps
        # showing them after a deploy or a logout. API answers hold call data: never stored.
        response.headers.setdefault('Cache-Control', 'no-store' if request.path.startswith('/api/') else 'no-cache')
        response.headers.update(SECURITY_HEADERS)

    app = web.Application()
    app[DB] = db
    app.cleanup_ctx.append(lifetime)
    app.on_response_prepare.append(harden)
    setup_auth(app, config.dashboard_password, secret, PUBLIC_DIR / 'login.html')
    setup_api(app, db, config, PUBLIC_DIR, upstream)
    app.router.add_get('/', index)
    app.router.add_get('/ws', call)
    app.router.add_static('/', PUBLIC_DIR)
    return app


def build_agent(cfg, settings, record, upstream, session_id, audio, *, send_control, send_audio, on_hangup):
    """The agent for one call in the pipeline cfg asks for, and its ElevenLabs client (None when it has none).

    classic: STT -> chat model -> ElevenLabs. realtime: one speech-to-speech session (OpenAI voices).
    hybrid: a Realtime session hears the caller and answers in text, spoken by ElevenLabs.
    live: a full-duplex GPT-Live session (OpenAI voices).
    """
    language = None if settings['language'] == 'auto' else settings['language']
    greeting = pick_greeting(settings, record.started_at)
    common = dict(
        send_control=send_control, send_audio=send_audio, on_hangup=on_hangup, greeting=greeting,
        silence_timeout=cfg.silence_timeout, max_call=cfg.max_call,
    )

    def make_tts():
        return TtsClient(
            api_key=cfg.elevenlabs_api_key, voice_id=settings['voice_id'], model_id=cfg.elevenlabs_model_id,
            ws_connect=upstream['ws_connect'], audio=audio, auto_mode=cfg.elevenlabs_auto_mode,
        )

    if cfg.pipeline == 'live':
        rate = cfg.pricing.get('live', {}).get(cfg.live_model, {}).get('per_minute')
        if rate is None:
            log.warning('[%s] [server] no pricing.json live rate for %s: this call is not costed', session_id, cfg.live_model)
        return LiveAgent(
            api_key=cfg.openai_api_key,
            model=cfg.live_model,
            instructions=build_system_prompt(
                settings['system_doc'], settings['owner_name'], settings['language'], hangup='goodbye',
            ),
            voice=cfg.live_voice,
            ws_connect=upstream['ws_connect'],
            session_id=session_id,
            on_usage=lambda seconds: record.set_live_usage(seconds, rate),
            **common,
        ), None

    if cfg.pipeline in ('realtime', 'hybrid'):
        rates = cfg.pricing.get('realtime', {}).get(cfg.agent_model)
        if not rates:
            log.warning('[%s] [server] no pricing.json realtime rates for %s: this call is not costed', session_id, cfg.agent_model)

        def on_usage(kind, usage):
            if kind == 'stt':
                record.add_stt_usage(usage)
            elif rates:
                record.add_realtime_usage(usage, rates)

        realtime = dict(
            api_key=cfg.openai_api_key,
            model=cfg.agent_model,
            instructions=build_system_prompt(
                settings['system_doc'], settings['owner_name'], settings['language'], hangup='tool',
            ),
            reasoning_effort=cfg.agent_reasoning,
            ws_connect=upstream['ws_connect'],
            language=language,
            turn_detection=turn_detection(cfg.vad_type, cfg.vad_silence_ms, cfg.vad_threshold, cfg.vad_eagerness),
            transcribe_model=cfg.openai_transcribe_model,
            session_id=session_id,
            on_usage=on_usage,
            **common,
        )
        if cfg.pipeline == 'realtime':
            return RealtimeAgent(voice=cfg.agent_voice, **realtime), None
        tts = make_tts()
        return HybridAgent(tts_client=tts, **realtime), tts

    if cfg.stt_provider == 'soniox':
        stt = SonioxSttClient(
            api_key=cfg.soniox_api_key,
            ws_connect=upstream['ws_connect'],
            language=language,
            vad_silence_ms=cfg.vad_silence_ms,
            endpoint_level=cfg.soniox_endpoint_level,
        )
    elif cfg.stt_provider == 'elevenlabs':
        stt = ElevenLabsSttClient(
            api_key=cfg.elevenlabs_api_key,
            ws_connect=upstream['ws_connect'],
            language=language,
            vad_silence_ms=cfg.vad_silence_ms,
            vad_threshold=cfg.vad_threshold,
        )
    else:
        stt = RealtimeSttClient(
            api_key=cfg.openai_api_key,
            model=cfg.openai_realtime_model,
            transcribe_model=cfg.openai_transcribe_model,
            ws_connect=upstream['ws_connect'],
            language=language,
            vad_silence_ms=cfg.vad_silence_ms,
            vad_threshold=cfg.vad_threshold,
            min_logprob=cfg.stt_min_logprob,
            vad_type=cfg.vad_type,
            vad_eagerness=cfg.vad_eagerness,
        )
    stt.on('usage', record.add_stt_usage)
    tts = make_tts()
    return SessionOrchestrator(
        system_prompt=build_system_prompt(settings['system_doc'], settings['owner_name'], settings['language']),
        stt_client=stt,
        llm_stream=partial(
            stream_completion,
            upstream['openai'],
            model=cfg.openai_chat_model,
            reasoning_effort=cfg.openai_reasoning_effort,
            on_usage=record.add_llm_usage,
        ),
        tts_client=tts,
        session_id=session_id,
        **common,
    ), tts


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = load_config()
    # No access log: call links carry their token in the query string.
    web.run_app(create_app(config), host=os.environ.get('HOST'), port=config.port, access_log=None)


if __name__ == '__main__':
    main()
