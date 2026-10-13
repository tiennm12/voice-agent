import asyncio
import base64
import json
import wave

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

from fakes import FakeOpenai, FakeWs
from voice_agent.config import load_config
from voice_agent.server import create_app

GREETING = 'Chào bạn, chủ máy hiện không thể nghe máy. Mình là trợ lý AI, bạn cần nhắn gì không ạ?'


class FakeUpstream:
    """Hands the server fake STT/TTS sockets and a fake OpenAI client."""

    def __init__(self, stt_error=None, **openai_kwargs):
        self.stt_error = stt_error
        self.openai = FakeOpenai(**openai_kwargs)
        self.stt = FakeWs()
        self.tts = []  # one socket per spoken turn
        self.tts_urls = []

    async def ws_connect(self, url, headers=None):
        if 'api.openai.com' not in url:
            self.tts_urls.append(url)
        if 'api.openai.com' in url:
            if self.stt_error:
                raise self.stt_error
            return self.stt
        self.tts.append(FakeWs())
        return self.tts[-1]

    async def tts_socket(self, index=0):
        """The TTS socket of the index-th spoken turn, once its text has been sent in full."""
        async with asyncio.timeout(2):
            # sent = [init message, text chunk(s)..., end-of-turn '']
            while len(self.tts) <= index or self.tts[index].sent[-1:] != [{'text': ''}]:
                await asyncio.sleep(0.005)
        return self.tts[index]


async def start(tmp_path, upstream):
    config = load_config(
        env={'OPENAI_API_KEY': 'k1', 'ELEVENLABS_API_KEY': 'k2', 'ELEVENLABS_VOICE_ID': 'v1', 'DATA_DIR': str(tmp_path)},
        read_system_doc=lambda: 'doc',
    )
    client = TestClient(TestServer(create_app(config, ws_connect=upstream.ws_connect, openai_client=upstream.openai)))
    await client.start_server()
    return client


@pytest.fixture
async def served(tmp_path):
    upstream = FakeUpstream()
    client = await start(tmp_path, upstream)
    yield client, upstream
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


async def saved_call(tmp_path, ready=lambda call: True):
    """The single call.json under tmp_path, once it exists and satisfies ready()."""
    async with asyncio.timeout(3):
        while True:
            for path in tmp_path.glob('calls/*/call.json'):
                call = json.loads(path.read_text(encoding='utf-8'))
                if ready(call):
                    return path.parent, call
            await asyncio.sleep(0.01)


async def test_serves_the_browser_client_with_script_content_types(served):
    client, _ = served

    page = await client.get('/')
    assert page.status == 200
    assert 'text/html' in page.headers['Content-Type']
    assert 'Voice Agent' in await page.text()

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
    client = await start(tmp_path, FakeUpstream(stt_error=OSError('dns failure')))
    try:
        ws = await client.ws_connect('/ws')
        frames = await receive_until(ws, lambda f: False)  # runs until the server closes
        assert frames == [{'type': 'error', 'message': 'Không thể kết nối tới dịch vụ giọng nói.'}]

        _, call = await saved_call(tmp_path, lambda call: call['summary'])
        assert 'không để lại lời nhắn' in call['summary']['summary']
    finally:
        await client.close()


async def test_a_full_voicemail_call_hangs_up_after_the_goodbye_and_is_saved_with_summary_and_cost(tmp_path):
    upstream = FakeUpstream(reply='Mình đã ghi nhận, chào bạn nhé. [HANGUP]')
    client = await start(tmp_path, upstream)
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

        call_dir, call = await saved_call(tmp_path, lambda call: call['cost'])
        assert call['caller'] == '0901234567'
        assert call['pbx_session_id'] == 'pbx-1'
        assert [(t['role'], t['text']) for t in call['transcript']] == [
            ('assistant', GREETING),
            ('user', 'Mình là Nam, nhờ anh Tuấn gọi lại.'),
            ('assistant', 'Mình đã ghi nhận, chào bạn nhé.'),
        ]
        assert call['summary'] == {'caller_name': 'Anh Nam', 'summary': 'Nhờ gọi lại về hợp đồng.', 'urgency': 'cao'}
        assert call['usage'] == {
            'llm_input_tokens': 100 + 200,  # the reply + the summary
            'llm_output_tokens': 10 + 40,
            'stt_text_tokens': 0,
            'stt_audio_tokens': 50,
            'stt_output_tokens': 12,
            # every chunk goes out with one trailing space, which replaces the one between chunks
            'tts_characters': len(GREETING + ' ') + len('Mình đã ghi nhận, chào bạn nhé. '),
        }
        assert call['cost']['total_usd'] > 0
        with wave.open(str(call_dir / 'caller.wav'), 'rb') as wav:
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
