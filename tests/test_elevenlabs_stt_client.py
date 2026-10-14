from fakes import FakeWs, fake_ws_connect, tick
from voice_agent.elevenlabs_stt_client import ElevenLabsSttClient


def make_client(ws, **kwargs):
    ws_connect, calls = fake_ws_connect(ws)
    return ElevenLabsSttClient(api_key='xi-test', ws_connect=ws_connect, **kwargs), calls


async def test_connect_puts_the_settings_in_the_url_and_authenticates_with_the_header():
    client, calls = make_client(FakeWs(), vad_silence_ms=400, vad_threshold=0.6)

    await client.connect()

    url, headers = calls[0]
    assert url.startswith('wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id=scribe_v2_realtime')
    for part in ('audio_format=pcm_24000', 'commit_strategy=vad', 'language_code=vi',
                 'vad_silence_threshold_secs=0.4', 'vad_threshold=0.6'):
        assert part in url
    assert headers == {'xi-api-key': 'xi-test'}


async def test_auto_language_leaves_the_language_out():
    client, calls = make_client(FakeWs(), language=None)

    await client.connect()

    assert 'language_code' not in calls[0][0]


async def test_audio_is_sent_as_base64_input_audio_chunks():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()

    await client.send_audio_chunk(b'\x01\x02')

    assert ws.sent[-1] == {
        'message_type': 'input_audio_chunk', 'audio_base_64': 'AQI=', 'commit': False, 'sample_rate': 24000,
    }


async def test_first_partial_is_speech_started_and_commit_is_speech_stopped_then_final():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    events = []
    for name in ('speech_started', 'speech_stopped'):
        client.on(name, lambda name=name: events.append(name))
    client.on('transcript_final', lambda text: events.append(('final', text)))

    ws.feed({'message_type': 'partial_transcript', 'text': 'xin'})
    ws.feed({'message_type': 'partial_transcript', 'text': 'xin chào'})
    ws.feed({'message_type': 'committed_transcript', 'text': 'Xin chào.'})
    ws.feed({'message_type': 'partial_transcript', 'text': 'tôi'})
    await tick()

    assert events == ['speech_started', 'speech_stopped', ('final', 'Xin chào.'), 'speech_started']


async def test_errors_are_emitted_and_close_reports_the_streamed_seconds_once():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    errors, usages = [], []
    client.on('error', errors.append)
    client.on('usage', usages.append)

    ws.feed({'message_type': 'quota_exceeded', 'error': 'no credits'})
    await client.send_audio_chunk(bytes(48000))  # one second of 24 kHz PCM16
    await tick()
    client.close()
    client.close()

    assert 'no credits' in str(errors[0])
    assert usages == [{'type': 'duration', 'seconds': 1.0}]
