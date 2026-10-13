import base64

from fakes import FakeWs, fake_ws_connect, tick
from voice_agent.tts_client import TtsClient


def make_client(*sockets):
    ws_connect, calls = fake_ws_connect(*sockets)
    client = TtsClient(api_key='xi-test', voice_id='voice123', model_id='eleven_flash_v2_5', ws_connect=ws_connect)
    return client, calls


def watch_disconnected(client):
    seen = []
    client.on('disconnected', lambda: seen.append(True))
    return seen


async def test_connect_opens_the_socket_with_the_right_url_and_headers_and_sends_the_init_message():
    ws = FakeWs()
    client, calls = make_client(ws)

    await client.connect()

    assert calls == [(
        'wss://api.elevenlabs.io/v1/text-to-speech/voice123/stream-input?model_id=eleven_flash_v2_5&output_format=mp3_44100_128',
        {'xi-api-key': 'xi-test'},
    )]
    assert ws.sent == [{'text': ' ', 'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75}}]


async def test_send_text_sends_a_text_message_and_end_turn_sends_an_empty_text_close_message():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    ws.sent.clear()

    await client.send_text('Xin chào.')
    await client.end_turn()

    assert ws.sent == [{'text': 'Xin chào. '}, {'text': ''}]


async def test_emits_audio_chunk_for_messages_with_audio_and_turn_complete_when_is_final():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    chunks, turn_complete = [], []
    client.on('audio_chunk', chunks.append)
    client.on('turn_complete', lambda: turn_complete.append(True))

    ws.feed({'audio': base64.b64encode(bytes([9, 9, 9])).decode()})
    ws.feed({'isFinal': True})
    await tick()

    assert chunks == [bytes([9, 9, 9])]
    assert turn_complete == [True]


async def test_abort_closes_the_socket_immediately():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()

    client.abort()
    await tick()

    assert ws.closed is True


async def test_send_text_after_abort_is_a_safe_no_op():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    client.abort()
    ws.sent.clear()

    await client.send_text('muộn rồi')

    assert ws.sent == []


async def test_emits_disconnected_when_the_socket_closes_unexpectedly_mid_turn():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    disconnected = watch_disconnected(client)

    ws.drop()  # no isFinal message and no abort() first -> unexpected
    await tick()

    assert disconnected == [True]


async def test_a_normal_end_of_turn_close_is_final_then_server_closes_does_not_emit_disconnected():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    disconnected = watch_disconnected(client)

    ws.feed({'isFinal': True})
    ws.drop()  # server closes the socket after the final message, as expected
    await tick()

    assert disconnected == []


async def test_abort_does_not_emit_disconnected_deliberate_shutdown_not_a_drop():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()
    disconnected = watch_disconnected(client)

    client.abort()
    await tick()

    assert disconnected == []


async def test_a_stale_socket_from_a_superseded_connect_closing_late_does_not_false_alarm_and_a_real_drop_is_still_reported():
    ws1, ws2 = FakeWs(), FakeWs()
    client, _ = make_client(ws1, ws2)
    await client.connect()  # turn 1 -> ws1
    ws1.feed({'isFinal': True})  # turn 1 ends normally
    await tick()
    await client.connect()  # turn 2 -> ws2
    disconnected = watch_disconnected(client)

    ws1.drop()  # turn 1's orphaned socket finally closes, after turn 2 already connected
    await tick()
    assert disconnected == [], 'an orphaned previous-turn socket closing must not false-alarm'

    ws2.drop()
    await tick()
    assert disconnected == [True], 'a real drop on the current connection must still be reported'


async def test_counts_the_characters_sent_for_cost_tracking():
    ws = FakeWs()
    client, _ = make_client(ws)
    await client.connect()

    await client.send_text('Xin chào.')
    await client.send_text('Bạn khỏe không?')
    await client.end_turn()

    assert client.characters_sent == len('Xin chào. ') + len('Bạn khỏe không? ')


async def test_pcm_audio_can_be_requested_for_clients_that_play_through_web_audio():
    ws = FakeWs()
    ws_connect, calls = fake_ws_connect(ws)
    client = TtsClient(
        api_key='xi-test', voice_id='voice123', model_id='eleven_flash_v2_5', ws_connect=ws_connect, audio='pcm',
    )

    await client.connect()

    assert calls[0][0].endswith('output_format=pcm_24000')
    assert client.bytes_per_second == 48000  # 24kHz, 16-bit mono


async def test_auto_mode_asks_elevenlabs_to_speak_each_chunk_as_it_arrives():
    ws = FakeWs()
    ws_connect, calls = fake_ws_connect(ws)
    client = TtsClient(
        api_key='xi-test', voice_id='voice123', model_id='eleven_flash_v2_5', ws_connect=ws_connect, auto_mode=True,
    )

    await client.connect()

    assert calls[0][0].endswith('output_format=mp3_44100_128&auto_mode=true')
