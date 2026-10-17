import pytest

from fakes import FakeWs, fake_ws_connect, tick
from voice_agent.soniox_stt_client import SonioxSttClient


def make_client(ws, **kwargs):
    ws_connect, calls = fake_ws_connect(ws)
    return SonioxSttClient(api_key=kwargs.pop('api_key', 'sx-test'), ws_connect=ws_connect, **kwargs), calls


def tokens(*items):
    """('text', is_final) pairs as one Soniox response."""
    return {'tokens': [{'text': text, 'is_final': final} for text, final in items]}


async def test_connect_authenticates_with_a_bearer_header_and_starts_a_vietnamese_stream_with_endpointing():
    ws = FakeWs()
    client, calls = make_client(ws, vad_silence_ms=400, endpoint_level=2)

    await client.connect()

    assert calls == [('wss://stt-rt.soniox.com/transcribe-websocket', {'Authorization': 'Bearer sx-test'})]
    assert ws.sent[0] == {
        'model': 'stt-rt-v5', 'audio_format': 'pcm_s16le', 'sample_rate': 24000, 'num_channels': 1,
        'language_hints': ['vi'], 'enable_endpoint_detection': True,
        'max_endpoint_delay_ms': 500,  # 400 is below Soniox's minimum
        'endpoint_latency_adjustment_level': 2,
    }


async def test_defaults_leave_the_endpoint_tuning_and_an_auto_language_out():
    ws = FakeWs()
    client, _ = make_client(ws, language=None)

    await client.connect()

    assert set(ws.sent[0]) == {'model', 'audio_format', 'sample_rate', 'num_channels', 'enable_endpoint_detection'}


async def test_connect_without_a_key_fails_at_once():
    client, calls = make_client(FakeWs(), api_key=None)

    with pytest.raises(RuntimeError, match='SONIOX_API_KEY'):
        await client.connect()
    assert calls == []


async def test_audio_is_sent_as_binary_frames():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()

    await client.send_audio_chunk(b'\x01\x02')

    assert ws.sent_bytes == [b'\x01\x02']


async def test_words_start_speech_and_the_end_token_delivers_only_the_final_text():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    events = []
    for name in ('speech_started', 'speech_stopped'):
        client.on(name, lambda name=name: events.append(name))
    client.on('transcript_final', lambda text: events.append(('final', text)))

    ws.feed(tokens(('Xin', False)))
    ws.feed(tokens(('Xin', True), (' chào', False)))  # partial text is replaced, never kept
    ws.feed(tokens((' chào', True), ('.', True), ('<end>', True)))
    ws.feed(tokens(('Tôi', False)))
    await tick()

    assert events == ['speech_started', 'speech_stopped', ('final', 'Xin chào.'), 'speech_started']


async def test_an_end_token_after_nothing_final_is_an_empty_transcript():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    finals = []
    client.on('transcript_final', finals.append)

    ws.feed(tokens(('<end>', True)))
    await tick()

    assert finals == [None]


async def test_errors_are_emitted_and_close_reports_the_streamed_seconds_once():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    errors, usages = [], []
    client.on('error', errors.append)
    client.on('usage', usages.append)

    ws.feed({'error_code': 401, 'error_message': 'Invalid API key.'})
    await client.send_audio_chunk(bytes(48000))  # one second of 24 kHz PCM16
    await tick()
    client.close()
    client.close()

    assert 'Invalid API key' in str(errors[0])
    assert usages == [{'type': 'duration', 'seconds': 1.0}]
