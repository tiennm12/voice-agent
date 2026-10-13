import asyncio
import base64
import json
import wave

import pytest
from aiohttp import WSMsgType, WSServerHandshakeError
from yarl import URL

from fakes import PASSWORD, FakeUpstream, start_app
from voice_agent import auth
from voice_agent.server import DB

GREETING = 'Chào bạn, chủ máy hiện không thể nghe máy. Mình là trợ lý AI, bạn cần nhắn gì không ạ?'


@pytest.fixture(autouse=True)
def no_login_delay(monkeypatch):
    monkeypatch.setattr(auth, 'LOGIN_FAILURE_DELAY', 0)


@pytest.fixture
async def served(tmp_path):
    client, upstream = await start_app(tmp_path)
    yield client, upstream
    await client.close()


@pytest.fixture
async def anonymous(tmp_path):
    client, _ = await start_app(tmp_path, login=False)
    yield client
    await client.close()


async def receive_until(ws, predicate, timeout=3):
    """Collect frames (parsed JSON for text, bytes for binary) until predicate(frame) is true or the socket closes."""
    frames = []
    async with asyncio.timeout(timeout):
        async for msg in ws:
            frames.append(json.loads(msg.data) if msg.type is WSMsgType.TEXT else msg.data)
            if predicate(frames[-1]):
                break
    return frames


async def saved_call(client, ready=lambda call: True):
    """The single call in the app's database, once it exists and satisfies ready()."""
    db = client.app[DB]
    async with asyncio.timeout(3):
        while True:
            for row in db.list_calls():
                call = db.get_call(row['id'])
                if ready(call):
                    return call
            await asyncio.sleep(0.01)


# --- login ---------------------------------------------------------------


async def test_pages_redirect_to_login_and_the_api_and_call_socket_answer_401_without_a_session(anonymous):
    for path in ('/', '/client.js', '/bridge.html'):
        response = await anonymous.get(path, allow_redirects=False)
        assert (response.status, response.headers.get('Location')) == (302, '/login'), path

    assert (await anonymous.get('/api/calls')).status == 401
    with pytest.raises(WSServerHandshakeError) as refused:
        await anonymous.ws_connect('/ws')
    assert refused.value.status == 401


async def test_the_login_page_is_reachable_without_a_session(anonymous):
    response = await anonymous.get('/login')

    assert response.status == 200
    assert 'name="password"' in await response.text()


async def test_a_wrong_password_gets_no_session(anonymous):
    response = await anonymous.post('/login', data={'password': 'guess'}, allow_redirects=False)

    assert (response.status, response.headers['Location']) == (302, '/login?error=1')
    assert 'session' not in response.cookies
    assert (await anonymous.get('/', allow_redirects=False)).status == 302


async def test_the_right_password_opens_a_session_in_a_hardened_cookie(anonymous):
    response = await anonymous.post('/login', data={'password': PASSWORD}, allow_redirects=False)

    assert (response.status, response.headers['Location']) == (302, '/')
    cookie = response.cookies['session']
    assert cookie['httponly'] and cookie['samesite'] == 'Strict'
    assert not cookie['secure'], 'plain-http localhost must still be able to log in'
    assert int(cookie['max-age']) == 30 * 24 * 3600
    assert (await anonymous.get('/', allow_redirects=False)).status == 200


async def test_the_session_cookie_is_secure_behind_https(anonymous):
    response = await anonymous.post(
        '/login', data={'password': PASSWORD}, headers={'X-Forwarded-Proto': 'https'}, allow_redirects=False,
    )

    assert response.cookies['session']['secure']


async def test_logout_ends_the_session(served):
    client, _ = served

    response = await client.post('/logout', allow_redirects=False)

    assert (response.status, response.headers['Location']) == (302, '/login')
    assert (await client.get('/', allow_redirects=False)).status == 302


async def test_a_call_socket_opened_from_another_site_is_refused_even_with_a_session(served):
    client, _ = served

    with pytest.raises(WSServerHandshakeError) as refused:
        await client.ws_connect('/ws', headers={'Origin': 'https://evil.example'})

    assert refused.value.status == 403


# --- static files ----------------------------------------------------------


async def test_serves_the_browser_client_with_script_content_types(served):
    client, _ = served

    page = await client.get('/')
    assert page.status == 200
    assert 'text/html' in page.headers['Content-Type']

    for path in ('/client.js', '/pcm-encoder-processor.js'):
        script = await client.get(path)
        assert script.status == 200
        # AudioWorklet.addModule() refuses anything that isn't a JavaScript MIME type.
        assert 'javascript' in script.headers['Content-Type'], path


async def test_does_not_serve_files_outside_public(served):
    client, _ = served

    response = await client.get(URL('/..%2F.env.example', encoded=True))

    assert response.status in (403, 404)
    assert 'OPENAI_API_KEY' not in await response.text()


# --- calls -----------------------------------------------------------------


async def test_a_call_speaks_the_fixed_greeting_and_relays_control_and_audio_in_order(served):
    client, upstream = served
    ws = await client.ws_connect('/ws')

    tts = await upstream.tts_socket()
    tts.feed({'audio': base64.b64encode(b'mp3-bytes').decode()})
    tts.feed({'isFinal': True})
    frames = await receive_until(ws, lambda f: f == {'type': 'state', 'state': 'listening'})

    assert frames == [
        {'type': 'state', 'state': 'speaking'},
        {'type': 'transcript', 'role': 'assistant', 'text': GREETING},
        b'mp3-bytes',
        {'type': 'state', 'state': 'listening'},
    ]
    assert tts.sent[1] == {'text': 'Chào bạn, '}
    assert upstream.openai.calls == [], 'the greeting must not cost an LLM call'
    await ws.close()


async def test_mic_audio_from_the_browser_is_forwarded_to_stt(served):
    client, upstream = served
    ws = await client.ws_connect('/ws')
    await upstream.tts_socket()  # the call is up

    await ws.send_bytes(bytes([1, 2, 3]))
    async with asyncio.timeout(2):
        while len(upstream.stt.sent) < 2:
            await asyncio.sleep(0.005)

    assert upstream.stt.sent[1] == {'type': 'input_audio_buffer.append', 'audio': base64.b64encode(bytes([1, 2, 3])).decode()}
    await ws.close()


async def test_hanging_up_closes_the_upstream_stt_and_tts_sockets(served):
    client, upstream = served
    ws = await client.ws_connect('/ws')
    tts = await upstream.tts_socket()

    await ws.close()
    async with asyncio.timeout(2):
        while not (upstream.stt.closed and tts.closed):
            await asyncio.sleep(0.005)


async def test_a_call_that_cannot_reach_stt_tells_the_browser_closes_and_is_still_recorded_as_a_missed_call(tmp_path):
    client, _ = await start_app(tmp_path, FakeUpstream(stt_error=OSError('dns failure')))
    try:
        ws = await client.ws_connect('/ws')
        frames = await receive_until(ws, lambda f: False)  # runs until the server closes
        assert frames == [{'type': 'error', 'message': 'Không thể kết nối tới dịch vụ giọng nói.'}]

        call = await saved_call(client, lambda call: call['summary'])
        assert 'không để lại lời nhắn' in call['summary']
    finally:
        await client.close()


async def test_a_full_voicemail_call_hangs_up_after_the_goodbye_and_is_saved_with_summary_and_cost(tmp_path):
    client, upstream = await start_app(tmp_path, FakeUpstream(reply='Mình đã ghi nhận, chào bạn nhé. [HANGUP]'))
    try:
        ws = await client.ws_connect('/ws')
        await ws.send_str(json.dumps({'type': 'callMeta', 'caller': '0901234567', 'pbxSessionId': 'pbx-1'}))
        await ws.send_bytes(bytes([7, 0, 8, 0]))

        greeting_tts = await upstream.tts_socket(0)
        greeting_tts.feed({'isFinal': True})
        await receive_until(ws, lambda f: f == {'type': 'state', 'state': 'listening'})

        upstream.stt.feed({
            'type': 'conversation.item.input_audio_transcription.completed',
            'transcript': 'Mình là Nam, nhờ anh Tuấn gọi lại.',
            'usage': {'type': 'tokens', 'input_tokens': 50, 'output_tokens': 12, 'input_token_details': {'audio_tokens': 50}},
        })
        goodbye_tts = await upstream.tts_socket(1)
        goodbye_tts.feed({'isFinal': True})

        # No predicate ever matches: this returns when the server hangs up on its own.
        frames = await receive_until(ws, lambda f: False)
        assert {'type': 'transcript', 'role': 'assistant', 'text': 'Mình đã ghi nhận, chào bạn nhé.'} in frames
        assert ws.closed

        call = await saved_call(client, lambda call: call['cost'])
        assert call['caller'] == '0901234567'
        assert call['pbx_session_id'] == 'pbx-1'
        assert call['read'] is False
        assert [(t['role'], t['text']) for t in call['transcript']] == [
            ('assistant', GREETING),
            ('user', 'Mình là Nam, nhờ anh Tuấn gọi lại.'),
            ('assistant', 'Mình đã ghi nhận, chào bạn nhé.'),
        ]
        assert (call['caller_name'], call['summary'], call['urgency']) == ('Anh Nam', 'Nhờ gọi lại về hợp đồng.', 'cao')
        assert call['usage'] == {
            'llm_input_tokens': 100 + 200,  # the reply + the summary
            'llm_output_tokens': 10 + 40,
            'stt_text_tokens': 0,
            'stt_audio_tokens': 50,
            'stt_output_tokens': 12,
            # every chunk goes out with one trailing space, which replaces the one between chunks
            'tts_characters': len(GREETING + ' ') + len('Mình đã ghi nhận, chào bạn nhé. '),
        }
        assert call['total_usd'] > 0
        with wave.open(str(tmp_path / 'calls' / call['id'] / 'caller.wav'), 'rb') as wav:
            assert wav.readframes(wav.getnframes()) == bytes([7, 0, 8, 0])
    finally:
        await client.close()


async def test_the_bridge_page_gets_pcm_audio_by_asking_for_it(served):
    client, upstream = served
    ws = await client.ws_connect('/ws?audio=pcm')

    await upstream.tts_socket()

    assert upstream.tts_urls[0].endswith('output_format=pcm_24000')
    await ws.close()


async def test_an_unknown_audio_format_falls_back_to_mp3(served):
    client, upstream = served
    ws = await client.ws_connect('/ws?audio=../../etc')

    await upstream.tts_socket()

    assert upstream.tts_urls[0].endswith('output_format=mp3_44100_128')
    await ws.close()


async def test_calls_past_the_retention_period_are_purged_when_the_server_starts(tmp_path):
    # First run records a call; RETENTION_DAYS=0 on the next start makes every finished call "old".
    client, upstream = await start_app(tmp_path)
    ws = await client.ws_connect('/ws')
    await upstream.tts_socket()
    await ws.close()
    call = await saved_call(client, lambda call: call['summary'])
    await client.close()
    assert (tmp_path / 'calls' / call['id'] / 'caller.wav').exists()

    client, _ = await start_app(tmp_path, RETENTION_DAYS='0')
    try:
        assert client.app[DB].list_calls() == []
        assert not (tmp_path / 'calls' / call['id']).exists()
    finally:
        await client.close()
