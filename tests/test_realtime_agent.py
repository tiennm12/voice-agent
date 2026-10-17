import asyncio
import base64
from types import SimpleNamespace

import pytest

from fakes import FakeTts, FakeWs, fake_ws_connect, tick
from voice_agent.call_record import compute_cost
from voice_agent.realtime_agent import HybridAgent, RealtimeAgent


def make_agent(ws, **kwargs):
    ws_connect, calls = fake_ws_connect(ws)
    controls, audio, hangups = [], [], []
    agent = RealtimeAgent(
        api_key='sk-test', model='gpt-realtime-2.1-mini', instructions='be brief', voice='marin',
        ws_connect=ws_connect, send_control=controls.append, send_audio=audio.append,
        on_hangup=lambda: hangups.append(True), **kwargs,
    )
    return agent, calls, controls, audio, hangups


async def test_start_configures_one_speech_to_speech_session_and_speaks_the_greeting():
    ws = FakeWs()
    agent, calls, *_ = make_agent(ws, greeting='Xin chào', turn_detection={'type': 'semantic_vad', 'eagerness': 'high'})

    await agent.start()
    agent.stop()

    assert calls == [('wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1-mini', {'Authorization': 'Bearer sk-test'})]
    session = ws.sent[0]['session']
    assert session['output_modalities'] == ['audio']
    assert session['audio']['output']['voice'] == 'marin'
    assert session['audio']['input']['turn_detection'] == {
        'type': 'semantic_vad', 'eagerness': 'high', 'create_response': True, 'interrupt_response': True,
    }
    assert session['tools'][0]['name'] == 'end_call'
    assert 'reasoning' not in session
    assert ws.sent[1]['type'] == 'response.create' and 'Xin chào' in ws.sent[1]['response']['instructions']


async def test_audio_out_state_transcripts_and_first_audio_latency():
    ws = FakeWs()
    agent, _, controls, audio, _ = make_agent(ws)
    await agent.start()

    ws.feed({'type': 'input_audio_buffer.speech_stopped'})
    ws.feed({'type': 'response.output_audio.delta', 'delta': base64.b64encode(b'\x01\x02').decode()})
    ws.feed({'type': 'response.output_audio_transcript.done', 'transcript': 'Dạ vâng'})
    ws.feed({'type': 'response.done', 'response': {'status': 'completed', 'output': []}})
    await tick()
    agent.stop()

    assert audio == [b'\x01\x02']
    kinds = [c['type'] for c in controls]
    assert kinds == ['state', 'latency', 'transcript', 'state']
    assert controls[0]['state'] == 'speaking' and controls[-1]['state'] == 'listening'


async def test_speech_started_flushes_playback():
    ws = FakeWs()
    agent, _, controls, *_ = make_agent(ws)
    await agent.start()

    ws.feed({'type': 'input_audio_buffer.speech_started'})
    await tick()
    agent.stop()

    assert controls == [{'type': 'interrupt'}]


async def test_end_call_tool_hangs_up_after_the_goodbye(monkeypatch):
    monkeypatch.setattr('voice_agent.realtime_agent.GOODBYE_GRACE', 0.01)
    ws = FakeWs()
    agent, _, _, _, hangups = make_agent(ws, silence_timeout=60)
    await agent.start()

    ws.feed({'type': 'response.done', 'response': {'status': 'completed', 'output': [{'type': 'function_call', 'name': 'end_call'}]}})
    await tick()
    assert hangups == []
    await asyncio.sleep(0.05)

    assert hangups == [True]
    agent.stop()


async def test_an_upstream_drop_ends_the_call_instead_of_leaving_dead_air():
    ws = FakeWs()
    agent, _, controls, _, hangups = make_agent(ws, silence_timeout=60)
    await agent.start()

    ws.feed({'type': 'input_audio_buffer.speech_started'})  # caller mid-sentence: no hangup timer armed
    ws.drop()
    await tick()

    assert any(c['type'] == 'error' for c in controls)
    assert hangups == [True]
    agent.stop()


# --- hybrid: realtime listens, answers in text, ElevenLabs speaks -------------


def make_hybrid(ws, tts=None, **kwargs):
    ws_connect, _ = fake_ws_connect(ws)
    ctx = SimpleNamespace(tts=tts or FakeTts(), controls=[], audio=[], hangups=[])
    ctx.agent = HybridAgent(
        api_key='sk-test', model='gpt-realtime-2.1-mini', instructions='be brief', tts_client=ctx.tts,
        ws_connect=ws_connect, send_control=ctx.controls.append, send_audio=ctx.audio.append,
        on_hangup=lambda: ctx.hangups.append(True), **kwargs,
    )
    return ctx


def reply(ws, *deltas, output=()):
    ws.feed({'type': 'response.created', 'response': {}})
    for delta in deltas:
        ws.feed({'type': 'response.output_text.delta', 'delta': delta})
    ws.feed({'type': 'response.output_text.done', 'text': ''.join(deltas)})
    ws.feed({'type': 'response.done', 'response': {'status': 'completed', 'output': list(output)}})


async def test_hybrid_asks_for_text_and_speaks_the_greeting_itself():
    ws = FakeWs()
    ctx = make_hybrid(ws, greeting='Xin chào, bạn cần nhắn gì không ạ?')

    await ctx.agent.start()
    await tick()

    session = ws.sent[0]['session']
    assert session['output_modalities'] == ['text']
    assert 'output' not in session['audio']
    assert session['tools'][0]['name'] == 'end_call'
    assert not any(m['type'] == 'response.create' for m in ws.sent), 'the greeting costs no model round trip'
    assert ws.sent[1]['item']['role'] == 'assistant'
    assert ws.sent[1]['item']['content'] == [{'type': 'output_text', 'text': 'Xin chào, bạn cần nhắn gì không ạ?'}]
    assert ctx.tts.sent_text == ['Xin chào,', 'bạn cần nhắn gì không ạ?']
    assert ctx.tts.end_turns == 1
    assert {'type': 'transcript', 'role': 'assistant', 'text': 'Xin chào, bạn cần nhắn gì không ạ?'} in ctx.controls
    ctx.agent.stop()


async def test_hybrid_speaks_the_text_reply_and_listens_once_tts_is_done(monkeypatch):
    ws = FakeWs()
    ctx = make_hybrid(ws, silence_timeout=60)
    await ctx.agent.start()

    ws.feed({'type': 'input_audio_buffer.speech_stopped'})
    reply(ws, 'Dạ, mình ', 'ghi nhận rồi.')
    await tick()

    assert ctx.tts.connects == 1
    assert ctx.tts.sent_text == ['Dạ,', 'mình ghi nhận rồi.']
    assert ctx.tts.end_turns == 1
    assert {'type': 'transcript', 'role': 'assistant', 'text': 'Dạ, mình ghi nhận rồi.'} in ctx.controls
    assert {'type': 'state', 'state': 'listening'} not in ctx.controls, 'still speaking until TTS is done'

    ctx.tts.emit('audio_chunk', b'\x01\x02')
    ctx.tts.emit('turn_complete')

    assert ctx.audio == [b'\x01\x02']
    latency = next(c for c in ctx.controls if c['type'] == 'latency')
    assert set(latency) == {'type', 'llm_ms', 'tts_ms', 'response_ms'}
    assert ctx.controls[-1] == {'type': 'state', 'state': 'listening'}
    ctx.agent.stop()


async def test_hybrid_barge_in_cuts_the_spoken_reply():
    ws = FakeWs()
    ctx = make_hybrid(ws)
    await ctx.agent.start()

    reply(ws, 'Một câu trả lời dài.')
    await tick()
    assert ctx.tts.end_turns == 1  # all text sent, audio still streaming back

    ws.feed({'type': 'input_audio_buffer.speech_started'})
    await tick()

    assert ctx.tts.aborts == 1
    assert {'type': 'interrupt'} in ctx.controls
    ctx.agent.stop()


async def test_hybrid_hangs_up_after_the_goodbye_has_been_spoken(monkeypatch):
    monkeypatch.setattr('voice_agent.realtime_agent.GOODBYE_GRACE', 0.01)
    ws = FakeWs()
    ctx = make_hybrid(ws, silence_timeout=60)
    await ctx.agent.start()

    reply(ws, 'Cảm ơn bạn, chào nhé.', output=[{'type': 'function_call', 'name': 'end_call'}])
    await tick()
    await asyncio.sleep(0.05)
    assert ctx.hangups == [], 'the goodbye is still being spoken'

    ctx.tts.emit('turn_complete')
    await asyncio.sleep(0.05)
    assert ctx.hangups == [True]
    ctx.agent.stop()


async def test_hybrid_end_call_without_words_hangs_up_at_once(monkeypatch):
    monkeypatch.setattr('voice_agent.realtime_agent.GOODBYE_GRACE', 0.01)
    ws = FakeWs()
    ctx = make_hybrid(ws, silence_timeout=60)
    await ctx.agent.start()

    ws.feed({'type': 'response.created', 'response': {}})
    ws.feed({'type': 'response.done', 'response': {'status': 'completed', 'output': [{'type': 'function_call', 'name': 'end_call'}]}})
    await tick()
    await asyncio.sleep(0.05)

    assert ctx.hangups == [True]
    assert ctx.tts.sent_text == []
    ctx.agent.stop()


def test_realtime_cost_prices_each_token_kind(tmp_path):
    from voice_agent.call_record import CallRecord

    rates = {
        'text_input': 4, 'cached_text_input': 0.4, 'text_output': 24,
        'audio_input': 32, 'cached_audio_input': 0.4, 'audio_output': 64,
    }
    record = CallRecord(session_id='x', data_dir=tmp_path, pricing={}, db=None, tz=None)
    record.add_realtime_usage({
        'input_token_details': {'text_tokens': 1_000_000, 'audio_tokens': 1_000_000, 'cached_tokens_details': {'text_tokens': 500_000}},
        'output_token_details': {'text_tokens': 1_000_000, 'audio_tokens': 1_000_000},
    }, rates)

    # 0.5M text at 4 + 0.5M cached at 0.4 + 1M audio at 32 + 1M text out at 24 + 1M audio out at 64
    assert record.usage['realtime_usd'] == pytest.approx(2 + 0.2 + 32 + 24 + 64)
    pricing = {
        'llm': {'input_per_1m_tokens': 0, 'output_per_1m_tokens': 0},
        'stt': {'audio_input_per_1m_tokens': 0, 'text_input_per_1m_tokens': 0, 'output_per_1m_tokens': 0, 'per_minute': 0},
        'tts': {'per_1k_characters': 0},
    }
    assert compute_cost(record.usage, pricing)['realtime_usd'] == pytest.approx(122.2)
