import asyncio
import base64
from types import SimpleNamespace

import pytest

from fakes import FakeWs, fake_ws_connect, tick
from voice_agent.live_agent import LiveAgent

VOICED = b'\x10\x00' * 2400  # 100 ms of quiet-but-not-silent PCM, as the model sends while speaking
SILENT = bytes(4800)  # 100 ms of exact zeros: the model's "not speaking"


def make(ws, **kwargs):
    ws_connect, calls = fake_ws_connect(ws)
    ctx = SimpleNamespace(calls=calls, controls=[], audio=[], hangups=[], usage=[])
    ctx.agent = LiveAgent(
        api_key='sk-test', model='gpt-live-1', instructions='be brief', voice='marin',
        ws_connect=ws_connect, send_control=ctx.controls.append, send_audio=ctx.audio.append,
        on_hangup=lambda: ctx.hangups.append(True), on_usage=ctx.usage.append, **kwargs,
    )
    return ctx


def audio(data):
    return {'type': 'session.output_audio.delta', 'delta': base64.b64encode(data).decode()}


def said(role, text):
    return {'type': f'session.{"input" if role == "user" else "output"}_transcript.delta', 'delta': text}


def decision(probability):
    return {
        'type': 'decision.event', 'delegation_id': None,
        'event': {'type': 'decision.completed', 'decision': {'answers': [
            {'type': 'predicate', 'name': 'call_over', 'probability': probability},
        ]}},
    }


async def test_start_opens_a_live_session_that_greets_first_and_watches_for_the_goodbye():
    ws = FakeWs()
    ctx = make(ws, greeting='Chào bạn, bạn cần nhắn gì không ạ?')

    await ctx.agent.start()
    ctx.agent.stop()

    assert ctx.calls == [('wss://api.openai.com/v1/live/sessions', {'Authorization': 'Bearer sk-test'})]
    start = ws.sent[0]
    assert start['type'] == 'session.start'
    session = start['session']
    assert session['model'] == 'gpt-live-1'
    assert session['audio'] == {'format': {'type': 'audio/pcm', 'rate': 24000}, 'output': {'voice': 'marin'}}
    # Told at startup, the model says the greeting word for word once the caller's audio flows.
    assert session['instructions'].startswith('be brief')
    assert 'Chào bạn, bạn cần nhắn gì không ạ?' in session['instructions']
    [trigger] = session['triggers']
    assert trigger['event_type'] == 'session.output_transcript.delta'
    assert trigger['decisions']['questions'][0]['name'] == 'call_over'


async def test_caller_audio_goes_up_as_base64():
    ws = FakeWs()
    ctx = make(ws)
    await ctx.agent.start()

    await ctx.agent.handle_audio_chunk(b'\x01\x02')
    ctx.agent.stop()

    assert ws.sent[1] == {'type': 'session.input_audio.append', 'audio': base64.b64encode(b'\x01\x02').decode()}


async def test_silence_from_the_model_is_dropped_and_speech_marks_the_speaking_state():
    ws = FakeWs()
    ctx = make(ws)
    await ctx.agent.start()

    for chunk in (SILENT, VOICED, VOICED, SILENT, SILENT):
        ws.feed(audio(chunk))
    await tick()
    ctx.agent.stop()

    assert ctx.audio == [VOICED, VOICED], 'exact-zero chunks are the model being quiet: never sent'
    assert [c for c in ctx.controls if c['type'] == 'state'] == [
        {'type': 'state', 'state': 'speaking'}, {'type': 'state', 'state': 'listening'},
    ]


async def test_the_reply_latency_counts_from_the_callers_last_voiced_audio():
    ws = FakeWs()
    ctx = make(ws)
    await ctx.agent.start()

    await ctx.agent.handle_audio_chunk(b'\x00\x40' * 480)  # the caller speaking
    await ctx.agent.handle_audio_chunk(bytes(960))  # then quiet
    await asyncio.sleep(0.05)
    ws.feed(audio(VOICED))
    await tick()
    ctx.agent.stop()

    [latency] = [c for c in ctx.controls if c['type'] == 'latency']
    assert latency['response_ms'] >= 40


async def test_transcript_deltas_are_joined_into_whole_turns():
    ws = FakeWs()
    ctx = make(ws)
    await ctx.agent.start()

    for event in (said('assistant', ' Chào'), said('assistant', ' bạn.'), said('user', ' Mình'), said('user', ' là Lan.'),
                  said('assistant', ' Dạ.')):
        ws.feed(event)
    await tick()
    ctx.agent.stop()  # flushes the turn still open

    assert [(c['role'], c['text']) for c in ctx.controls if c['type'] == 'transcript'] == [
        ('assistant', 'Chào bạn.'), ('user', 'Mình là Lan.'), ('assistant', 'Dạ.'),
    ]


async def test_a_confident_goodbye_decision_hangs_up_after_the_goodbye_plays(monkeypatch):
    monkeypatch.setattr('voice_agent.live_agent.GOODBYE_GRACE', 0.01)
    ws = FakeWs()
    ctx = make(ws, silence_timeout=60)
    await ctx.agent.start()

    ws.feed(decision(0.3))
    await tick()
    await asyncio.sleep(0.05)
    assert ctx.hangups == [], 'an unsure decision is not a goodbye'

    ws.feed(audio(VOICED))  # 100 ms of goodbye still to play
    ws.feed(decision(0.95))
    await tick()
    await asyncio.sleep(0.05)
    assert ctx.hangups == [], 'the caller is still hearing the goodbye'
    await asyncio.sleep(0.12)
    assert ctx.hangups == [True]
    ctx.agent.stop()


async def test_the_caller_talking_after_the_goodbye_keeps_the_call_open(monkeypatch):
    monkeypatch.setattr('voice_agent.live_agent.GOODBYE_GRACE', 0.05)
    ws = FakeWs()
    ctx = make(ws, silence_timeout=60)
    await ctx.agent.start()

    ws.feed(decision(0.95))
    ws.feed(said('user', ' À quên'))
    await tick()
    await asyncio.sleep(0.1)

    assert ctx.hangups == []
    ctx.agent.stop()


async def test_hangs_up_when_nobody_speaks():
    ws = FakeWs()
    ctx = make(ws, silence_timeout=0.05)
    await ctx.agent.start()

    await asyncio.sleep(0.15)

    assert ctx.hangups == [True]
    ctx.agent.stop()


async def test_an_upstream_drop_ends_the_call():
    ws = FakeWs()
    ctx = make(ws, silence_timeout=60)
    await ctx.agent.start()

    ws.drop()
    await tick()

    assert any(c['type'] == 'error' for c in ctx.controls)
    assert ctx.hangups == [True]
    ctx.agent.stop()


async def test_stop_reports_the_session_length_for_billing():
    ws = FakeWs()
    ctx = make(ws)
    await ctx.agent.start()
    await asyncio.sleep(0.05)

    ctx.agent.stop()
    ctx.agent.stop()

    [seconds] = ctx.usage
    assert 0.04 <= seconds < 1  # a busy machine oversleeps; it must never undercount
