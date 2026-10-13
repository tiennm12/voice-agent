import base64

from fakes import FakeWs, fake_ws_connect, tick
from voice_agent.realtime_stt_client import RealtimeSttClient


def make_client(*sockets):
    ws_connect, calls = fake_ws_connect(*sockets)
    client = RealtimeSttClient(
        api_key='sk-test', model='gpt-realtime-2.1', transcribe_model='gpt-4o-transcribe', ws_connect=ws_connect,
    )
    return client, calls


def watch_disconnected(client):
    seen = []
    client.on('disconnected', lambda: seen.append(True))
    return seen


async def test_connect_opens_the_socket_with_the_auth_header_and_sends_session_update():
    ws = FakeWs()
    client, calls = make_client(ws)

    await client.connect()

    assert calls == [('wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1', {'Authorization': 'Bearer sk-test'})]
    assert len(ws.sent) == 1
    assert ws.sent[0]['type'] == 'session.update'
    audio_input = ws.sent[0]['session']['audio']['input']
    assert audio_input['format'] == {'type': 'audio/pcm', 'rate': 24000}
    assert audio_input['turn_detection']['type'] == 'server_vad'
    assert audio_input['turn_detection']['create_response'] is False
    assert audio_input['transcription'] == {'model': 'gpt-4o-transcribe', 'language': 'vi'}
    assert audio_input['noise_reduction']['type'] == 'near_field'


async def test_emits_speech_started_and_transcript_final_from_incoming_server_events():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    events = []
    client.on('speech_started', lambda: events.append(('speech_started',)))
    client.on('transcript_final', lambda t: events.append(('transcript_final', t)))

    ws.feed({'type': 'input_audio_buffer.speech_started'})
    ws.feed({'type': 'conversation.item.input_audio_transcription.completed', 'transcript': 'Xin chào'})
    await tick()

    assert events == [('speech_started',), ('transcript_final', 'Xin chào')]


async def test_emits_error_for_a_realtime_api_error_event():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    errors = []
    client.on('error', lambda err: errors.append(str(err)))

    ws.feed({'type': 'error', 'error': {'message': 'bad session'}})
    await tick()

    assert errors == ['bad session']


async def test_send_audio_chunk_sends_base64_encoded_audio_in_an_input_audio_buffer_append_event():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    ws.sent.clear()

    await client.send_audio_chunk(bytes([1, 2, 3]))

    assert ws.sent == [{'type': 'input_audio_buffer.append', 'audio': base64.b64encode(bytes([1, 2, 3])).decode()}]


async def test_send_audio_chunk_before_the_socket_is_open_is_a_safe_no_op():
    ws = FakeWs()
    client, _ = make_client(ws)

    await client.send_audio_chunk(bytes([1, 2, 3]))

    assert ws.sent == []


async def test_emits_disconnected_when_the_socket_closes_unexpectedly():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    disconnected = watch_disconnected(client)

    ws.drop()
    await tick()

    assert disconnected == [True]


async def test_close_does_not_emit_disconnected_deliberate_shutdown_not_a_drop():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    disconnected = watch_disconnected(client)

    client.close()
    await tick()

    assert disconnected == []


async def test_a_stale_socket_from_a_superseded_connect_closing_late_does_not_false_alarm_and_a_real_drop_is_still_reported():
    ws1, ws2 = FakeWs(), FakeWs()
    client, _ = make_client(ws1, ws2)
    await client.connect()  # first connection -> ws1
    await client.connect()  # reconnect -> ws2
    disconnected = watch_disconnected(client)

    ws1.drop()  # the orphaned first socket's close arrives late
    await tick()
    assert disconnected == [], 'an orphaned previous-connection socket closing must not false-alarm'

    ws2.drop()
    await tick()
    assert disconnected == [True], 'a real drop on the current connection must still be reported'


async def test_close_closes_the_underlying_socket():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()

    client.close()
    await tick()

    assert ws.closed is True


async def test_emits_speech_stopped_when_the_caller_pauses():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    events = []
    client.on('speech_stopped', lambda: events.append('speech_stopped'))

    ws.feed({'type': 'input_audio_buffer.speech_stopped'})
    await tick()

    assert events == ['speech_stopped']


async def test_emits_the_transcription_usage_for_cost_tracking():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    usages = []
    client.on('usage', usages.append)
    client.on('transcript_final', lambda text: None)
    usage = {'type': 'tokens', 'input_tokens': 40, 'output_tokens': 12, 'input_token_details': {'audio_tokens': 30, 'text_tokens': 10}}

    ws.feed({'type': 'conversation.item.input_audio_transcription.completed', 'transcript': 'Xin chào', 'usage': usage})
    await tick()

    assert usages == [usage]
