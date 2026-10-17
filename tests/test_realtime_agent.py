import asyncio
import base64

import pytest

from fakes import FakeWs, fake_ws_connect, tick
from voice_agent.call_record import compute_cost
from voice_agent.realtime_agent import RealtimeAgent


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
