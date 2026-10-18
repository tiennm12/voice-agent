import asyncio
import base64
import json
import wave

import pytest
from aiohttp import WSMsgType, WSServerHandshakeError
from yarl import URL

from fakes import PASSWORD, FakeHttp, FakeUpstream, start_app
from voice_agent import auth
from voice_agent.server import DB

GREETING = 'Chào bạn, chủ máy hiện không thể nghe máy. Mình là trợ lý AI, cuộc gọi này được ghi âm để chuyển lời nhắn. Bạn cần nhắn gì không ạ?'


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


async def test_browsers_must_revalidate_pages_and_never_store_api_answers(served, anonymous):
    client, _ = served

    # Without this a browser keeps showing a cached page after a deploy — or after logout.
    for path in ('/', '/client.js', '/login'):
        assert (await client.get(path)).headers['Cache-Control'] == 'no-cache', path
    assert (await client.get('/api/calls')).headers['Cache-Control'] == 'no-store'
    redirect = await anonymous.get('/', allow_redirects=False)
    assert redirect.headers['Cache-Control'] == 'no-cache'


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
        # Not "no message": nobody could hear the caller, and the owner must know to call back.
        assert 'Hệ thống gặp lỗi' in call['summary']
        assert call['urgency'] == 'trung bình'
    finally:
        await client.close()


async def test_an_upstream_handshake_that_hangs_gives_up_in_seconds(tmp_path, monkeypatch):
    from voice_agent import server

    monkeypatch.setattr(server, 'CONNECT_TIMEOUT', 0.05)

    class HangingUpstream(FakeUpstream):
        async def ws_connect(self, url, headers=None):
            await asyncio.sleep(3600)

    client, _ = await start_app(tmp_path, HangingUpstream())
    try:
        ws = await client.ws_connect('/ws')
        frames = await receive_until(ws, lambda f: False, timeout=2)
        assert frames == [{'type': 'error', 'message': 'Không thể kết nối tới dịch vụ giọng nói.'}]
    finally:
        await client.close()


async def test_a_call_whose_stt_drops_before_the_caller_speaks_is_filed_as_a_system_failure(served):
    client, upstream = served
    ws = await client.ws_connect('/ws')
    await upstream.tts_socket()

    upstream.stt.drop()
    await receive_until(ws, lambda f: False)  # the agent hangs up

    call = await saved_call(client, lambda call: call['summary'])
    assert 'Hệ thống gặp lỗi' in call['summary']


async def test_a_caller_who_stays_silent_still_leaves_no_message(served):
    client, upstream = served
    ws = await client.ws_connect('/ws')
    await upstream.tts_socket()
    await ws.close()

    call = await saved_call(client, lambda call: call['summary'])
    assert call['summary'] == 'Người gọi không để lại lời nhắn.'


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


async def test_the_hybrid_pipeline_listens_with_realtime_and_speaks_with_elevenlabs(tmp_path):
    client, upstream = await start_app(tmp_path)
    try:
        ws = await client.ws_connect('/ws?pipeline=hybrid')
        await upstream.tts_socket(0)  # the greeting, spoken by ElevenLabs

        session = upstream.stt.sent[0]['session']  # the one OpenAI socket is the Realtime session
        assert session['output_modalities'] == ['text']
        assert upstream.stt.sent[1]['type'] == 'conversation.item.create'
        await ws.close()

        call = await saved_call(client, lambda call: call['summary'])
        assert call['usage']['tts_characters'] > len(GREETING)
        assert call['transcript'][0] == {**call['transcript'][0], 'role': 'assistant', 'text': GREETING}
    finally:
        await client.close()


async def test_the_live_pipeline_runs_one_gpt_live_session_and_prices_it_by_the_minute(tmp_path):
    client, upstream = await start_app(tmp_path)
    try:
        ws = await client.ws_connect('/ws?pipeline=live&live_voice=quartz')
        async with asyncio.timeout(2):
            while not upstream.stt.sent:
                await asyncio.sleep(0.005)
        start = upstream.stt.sent[0]  # the one OpenAI socket is the Live session
        assert start['type'] == 'session.start'
        assert start['session']['audio']['output']['voice'] == 'quartz'
        assert 'hệ thống sẽ tự cúp máy' in start['session']['instructions']
        assert upstream.tts == [], 'no ElevenLabs'
        await ws.close()

        call = await saved_call(client, lambda call: call['summary'])
        assert 'live_seconds' in call['usage']
        assert call['cost']['realtime_usd'] == pytest.approx(call['usage']['live_seconds'] / 60 * 0.05, abs=1e-4)
    finally:
        await client.close()


async def test_soniox_stt_can_be_picked_per_call(tmp_path):
    client, upstream = await start_app(tmp_path, SONIOX_API_KEY='sx-key')
    try:
        ws = await client.ws_connect('/ws?stt_provider=soniox&soniox_endpoint_level=2')
        await upstream.tts_socket(1)  # the greeting; FakeUpstream files every non-OpenAI socket under tts
        assert upstream.tts_urls[0] == 'wss://stt-rt.soniox.com/transcribe-websocket'
        assert upstream.tts[0].sent[0]['endpoint_latency_adjustment_level'] == 2
        assert upstream.stt.sent == [], 'OpenAI transcription is not used'
        await ws.close()
    finally:
        await client.close()


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


async def test_a_call_the_server_stopped_before_summarizing_is_summarized_and_announced_on_the_next_start(tmp_path):
    from datetime import datetime, timedelta, timezone

    started = datetime.now(timezone(timedelta(hours=7))) - timedelta(minutes=5)
    client, _ = await start_app(tmp_path)
    client.app[DB].save_call({
        'id': f'{started:%Y%m%dT%H%M%S}_cccccccc', 'session_id': 'cccccccc', 'caller': '0901234567',
        'started_at': started.isoformat(), 'ended_at': (started + timedelta(seconds=20)).isoformat(),
        'duration_seconds': 20, 'usage': {'llm_input_tokens': 100},
        'transcript': [{'role': 'user', 'text': 'Mình là Nam, nhờ gọi lại.', 'at': started.isoformat()}],
    })
    await client.close()

    http = FakeHttp()
    client, _ = await start_app(tmp_path, http=http, TELEGRAM_BOT_TOKEN='TOKEN', TELEGRAM_CHAT_ID='42')
    try:
        call = await saved_call(client, lambda call: call['summary'])
        assert call['caller_name'] == 'Anh Nam'
        assert call['usage']['llm_input_tokens'] == 100 + 200
        async with asyncio.timeout(3):
            while not http.posts:
                await asyncio.sleep(0.01)
        assert 'Cuộc gọi nhỡ từ 0901234567' in http.posts[0][1]['text']
    finally:
        await client.close()


# --- signed call links (the button under a Telegram notification) -----------

CALL_ID = '20261013T090000_bbbbbbbb'
OTHER_ID = '20261012T090000_aaaaaaaa'


def seed_call(client, tmp_path, call_id):
    client.app[DB].save_call({
        'id': call_id, 'session_id': call_id[-8:], 'caller': '0901234567',
        'started_at': '2026-10-13T09:00:00+07:00', 'ended_at': '2026-10-13T09:00:20+07:00', 'duration_seconds': 20,
        'summary': 'Nhờ gọi lại.', 'transcript': [], 'usage': {},
    })
    folder = tmp_path / 'calls' / call_id
    folder.mkdir(parents=True)
    (folder / 'caller.wav').write_bytes(b'RIFFdata')


def link_query(tmp_path, call_id, lifetime=3600):
    return {'t': auth.make_token(auth.load_secret(tmp_path), f'call:{call_id}', lifetime)}


async def test_a_call_link_opens_that_call_without_a_session(anonymous, tmp_path):
    seed_call(anonymous, tmp_path, CALL_ID)
    query = link_query(tmp_path, CALL_ID)

    page = await anonymous.get(f'/c/{CALL_ID}', params=query)
    assert page.status == 200
    assert 'call-view.js' in await page.text()
    for asset in ('/app.css', '/call-view.js'):
        assert (await anonymous.get(asset)).status == 200, f'{asset} must load for someone who is not logged in'

    call = await anonymous.get(f'/api/calls/{CALL_ID}', params=query)
    assert (await call.json())['summary'] == 'Nhờ gọi lại.'
    audio = await anonymous.get(f'/api/calls/{CALL_ID}/audio', params=query)
    assert await audio.read() == b'RIFFdata'

    marked = await anonymous.post(f'/api/calls/{CALL_ID}/read', params=query, json={'read': True})
    assert marked.status == 200
    assert anonymous.app[DB].get_call(CALL_ID)['read'] is True


async def test_a_call_link_opens_nothing_but_its_own_call(anonymous, tmp_path):
    seed_call(anonymous, tmp_path, CALL_ID)
    seed_call(anonymous, tmp_path, OTHER_ID)
    query = link_query(tmp_path, CALL_ID)

    assert (await anonymous.get(f'/api/calls/{OTHER_ID}', params=query)).status == 401
    assert (await anonymous.get(f'/api/calls/{OTHER_ID}/audio', params=query)).status == 401
    assert (await anonymous.get(f'/c/{OTHER_ID}', params=query, allow_redirects=False)).status == 302
    for path in ('/api/calls', '/api/settings', '/api/stats'):
        assert (await anonymous.get(path, params=query)).status == 401, path
    assert (await anonymous.put('/api/settings', params=query, json={'greeting': 'hacked'})).status == 401
    assert (await anonymous.get('/', params=query, allow_redirects=False)).status == 302


async def test_an_expired_or_forged_call_link_is_refused(anonymous, tmp_path):
    seed_call(anonymous, tmp_path, CALL_ID)

    for query in (link_query(tmp_path, CALL_ID, lifetime=-10), {'t': '9999999999.deadbeef'}, {'t': ''}, {}):
        assert (await anonymous.get(f'/api/calls/{CALL_ID}', params=query)).status == 401, query


async def test_knowing_the_password_is_not_enough_to_forge_a_call_link(anonymous, tmp_path):
    # Links travel through Telegram. Were they signed with a key derived from the password,
    # anyone holding one could guess passwords offline against its signature.
    import hashlib

    seed_call(anonymous, tmp_path, CALL_ID)
    for derived in (PASSWORD.encode(), hashlib.sha256(PASSWORD.encode()).digest(),
                    hashlib.sha256(b'voice-agent-session:' + PASSWORD.encode()).digest()):
        forged = {'t': auth.make_token(derived, f'call:{CALL_ID}', 3600)}
        assert (await anonymous.get(f'/api/calls/{CALL_ID}', params=forged)).status == 401


async def test_sessions_and_links_survive_a_restart_but_a_new_password_ends_sessions(tmp_path):
    client, _ = await start_app(tmp_path)
    session = client.session.cookie_jar.filter_cookies(client.make_url('/'))['session'].value
    seed_call(client, tmp_path, CALL_ID)
    query = link_query(tmp_path, CALL_ID)
    await client.close()

    restarted, _ = await start_app(tmp_path, login=False)
    try:
        assert (await restarted.get('/api/calls', cookies={'session': session})).status == 200
        assert (await restarted.get(f'/api/calls/{CALL_ID}', params=query)).status == 200
    finally:
        await restarted.close()

    new_password, _ = await start_app(tmp_path, login=False, DASHBOARD_PASSWORD='a different password')
    try:
        assert (await new_password.get('/api/calls', cookies={'session': session})).status == 401
        assert (await new_password.get(f'/api/calls/{CALL_ID}', params=query)).status == 200, 'links keep working'
    finally:
        await new_password.close()


async def test_a_finished_call_is_announced_on_telegram_with_a_working_link(tmp_path):
    http = FakeHttp()
    client, upstream = await start_app(
        tmp_path, http=http, login=False,
        TELEGRAM_BOT_TOKEN='TOKEN', TELEGRAM_CHAT_ID='42', PUBLIC_BASE_URL='https://vm.example/',
    )
    try:
        await client.post('/login', data={'password': PASSWORD}, allow_redirects=False)
        ws = await client.ws_connect('/ws')
        await ws.send_str(json.dumps({'type': 'callMeta', 'caller': '0901234567'}))
        await upstream.tts_socket()
        await ws.close()
        async with asyncio.timeout(3):
            while not http.posts:
                await asyncio.sleep(0.01)

        url, payload = http.posts[0]
        assert url == 'https://api.telegram.org/botTOKEN/sendMessage'
        assert 'Cuộc gọi nhỡ từ 0901234567' in payload['text']
        link = payload['reply_markup']['inline_keyboard'][0][0]['url']
        assert link.startswith('https://vm.example/c/2'), link

        await client.post('/logout', allow_redirects=False)
        opened = await client.get(link.removeprefix('https://vm.example'))
        assert opened.status == 200
    finally:
        await client.close()


async def test_the_configured_chat_model_and_vad_tuning_reach_the_upstreams(tmp_path):
    client, upstream = await start_app(
        tmp_path, FakeUpstream(reply='Dạ.'), OPENAI_CHAT_MODEL='gpt-4.1-nano', VAD_SILENCE_MS='300',
    )
    try:
        ws = await client.ws_connect('/ws')
        greeting_tts = await upstream.tts_socket(0)
        greeting_tts.feed({'isFinal': True})
        upstream.stt.feed({'type': 'conversation.item.input_audio_transcription.completed', 'transcript': 'Alo'})
        await upstream.tts_socket(1)

        chat = upstream.openai.calls[0]
        assert chat['model'] == 'gpt-4.1-nano'
        assert 'reasoning_effort' not in chat, 'a model chosen by hand does not get the default model\'s setting'
        assert upstream.stt.sent[0]['session']['audio']['input']['turn_detection']['silence_duration_ms'] == 300
        await ws.close()
    finally:
        await client.close()


async def test_by_default_both_the_reply_and_the_summary_use_gpt_6_luna_without_reasoning(tmp_path):
    client, upstream = await start_app(tmp_path, FakeUpstream(reply='Dạ, mình ghi nhận rồi.'))
    try:
        ws = await client.ws_connect('/ws')
        greeting_tts = await upstream.tts_socket(0)
        greeting_tts.feed({'isFinal': True})
        upstream.stt.feed({'type': 'conversation.item.input_audio_transcription.completed', 'transcript': 'Alo'})
        await upstream.tts_socket(1)
        await ws.close()
        await saved_call(client, lambda call: call['summary'])

        reply, summary = upstream.openai.calls
        assert (reply['model'], reply['reasoning_effort'], reply['stream']) == ('gpt-6-luna', 'none', True)
        assert (summary['model'], summary['reasoning_effort']) == ('gpt-6-luna', 'none')
        assert summary['response_format']['type'] == 'json_schema'
    finally:
        await client.close()
