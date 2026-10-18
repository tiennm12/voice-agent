import io

import pytest
from aiohttp import FormData

from fakes import FakeHttp, FakeUpstream, start_app
from voice_agent.server import DB

DEFAULT_GREETING = 'Chào bạn, chủ máy hiện không thể nghe máy. Mình là trợ lý AI, cuộc gọi này được ghi âm để chuyển lời nhắn. Bạn cần nhắn gì không ạ?'


@pytest.fixture
async def client(tmp_path):
    client, _ = await start_app(tmp_path)
    yield client
    await client.close()


def seed(client, call_id, started_at, **fields):
    client.app[DB].save_call({
        'id': call_id,
        'session_id': call_id[-8:],
        'caller': '0901234567',
        'started_at': started_at,
        'ended_at': started_at,
        'duration_seconds': 21,
        'transcript': [{'role': 'user', 'text': 'Alo, mình là Nam.', 'at': started_at}],
        'usage': {'tts_characters': 88},
        **fields,
    })


# --- calls -----------------------------------------------------------------


async def test_the_inbox_lists_calls_newest_first_without_transcripts(client):
    seed(client, '20261012T090000_aaaaaaaa', '2026-10-12T09:00:00+07:00', summary='Cũ hơn.')
    seed(client, '20261013T090000_bbbbbbbb', '2026-10-13T09:00:00+07:00', summary='Mới hơn.', urgency='cao',
         caller_name='Nam', cost={'total_usd': 0.0115})

    response = await client.get('/api/calls')

    assert response.status == 200
    calls = (await response.json())['calls']
    assert [c['id'] for c in calls] == ['20261013T090000_bbbbbbbb', '20261012T090000_aaaaaaaa']
    assert calls[0] == {
        'id': '20261013T090000_bbbbbbbb', 'caller': '0901234567', 'started_at': '2026-10-13T09:00:00+07:00',
        'duration_seconds': 21, 'read': False, 'caller_name': 'Nam', 'summary': 'Mới hơn.', 'urgency': 'cao',
        'category': None, 'total_usd': 0.0115,
    }


async def test_the_inbox_pages_and_filters(client):
    for day in (10, 11, 12, 13):
        seed(client, f'202610{day}T090000_aaaaaaaa', f'2026-10-{day}T09:00:00+07:00',
             urgency='cao' if day == 11 else 'thấp')
    client.app[DB].mark_read('20261013T090000_aaaaaaaa')

    first = (await (await client.get('/api/calls', params={'limit': '2'})).json())['calls']
    second = (await (await client.get(
        '/api/calls', params={'limit': '2', 'before': first[-1]['started_at']},
    )).json())['calls']
    assert [c['id'][:8] for c in first + second] == ['20261013', '20261012', '20261011', '20261010']

    urgent = (await (await client.get('/api/calls', params={'filter': 'urgent'})).json())['calls']
    unread = (await (await client.get('/api/calls', params={'filter': 'unread'})).json())['calls']
    assert [c['id'][:8] for c in urgent] == ['20261011']
    assert len(unread) == 3


@pytest.mark.parametrize('params', [{'filter': 'everything'}, {'limit': 'ten'}, {'limit': '0'}, {'limit': '5000'}])
async def test_the_inbox_rejects_bad_query_parameters(client, params):
    assert (await client.get('/api/calls', params=params)).status == 400


async def test_a_call_opens_with_its_transcript_and_can_be_marked_read_and_unread(client):
    seed(client, '20261013T090000_bbbbbbbb', '2026-10-13T09:00:00+07:00', summary='Nhờ gọi lại.')

    call = await (await client.get('/api/calls/20261013T090000_bbbbbbbb')).json()
    assert call['transcript'] == [{'role': 'user', 'text': 'Alo, mình là Nam.', 'at': '2026-10-13T09:00:00+07:00'}]
    assert call['read'] is False, 'looking is not reading: the page says so explicitly'

    marked = await client.post('/api/calls/20261013T090000_bbbbbbbb/read', json={'read': True})
    assert marked.status == 200
    assert (await (await client.get('/api/calls/20261013T090000_bbbbbbbb')).json())['read'] is True

    await client.post('/api/calls/20261013T090000_bbbbbbbb/read', json={'read': False})
    assert (await (await client.get('/api/calls/20261013T090000_bbbbbbbb')).json())['read'] is False


async def test_unknown_calls_are_404(client):
    assert (await client.get('/api/calls/20261013T090000_ffffffff')).status == 404
    assert (await client.post('/api/calls/20261013T090000_ffffffff/read', json={'read': True})).status == 404
    assert (await client.get('/api/calls/20261013T090000_ffffffff/audio')).status == 404


async def test_marking_read_needs_a_boolean(client):
    seed(client, '20261013T090000_bbbbbbbb', '2026-10-13T09:00:00+07:00')

    assert (await client.post('/api/calls/20261013T090000_bbbbbbbb/read', json={'read': 'yes'})).status == 400
    assert (await client.post('/api/calls/20261013T090000_bbbbbbbb/read', data=b'not json')).status == 400


async def test_the_recording_streams_as_audio_and_supports_seeking(client, tmp_path):
    seed(client, '20261013T090000_bbbbbbbb', '2026-10-13T09:00:00+07:00')
    folder = tmp_path / 'calls' / '20261013T090000_bbbbbbbb'
    folder.mkdir(parents=True)
    (folder / 'caller.wav').write_bytes(b'RIFF' + bytes(range(100)))

    whole = await client.get('/api/calls/20261013T090000_bbbbbbbb/audio')
    assert whole.status == 200
    assert whole.headers['Content-Type'] == 'audio/wav'
    assert await whole.read() == b'RIFF' + bytes(range(100))

    part = await client.get('/api/calls/20261013T090000_bbbbbbbb/audio', headers={'Range': 'bytes=4-7'})
    assert part.status == 206
    assert await part.read() == bytes([0, 1, 2, 3])


async def test_a_call_whose_recording_is_gone_answers_404_for_audio(client):
    seed(client, '20261013T090000_bbbbbbbb', '2026-10-13T09:00:00+07:00')

    assert (await client.get('/api/calls/20261013T090000_bbbbbbbb/audio')).status == 404


# --- stats -----------------------------------------------------------------


async def test_stats_count_calls_and_cost_per_period(client):
    seed(client, '20261012T090000_aaaaaaaa', '2026-10-12T09:00:00+07:00', urgency='cao', cost={'total_usd': 0.01})
    seed(client, '20261013T090000_bbbbbbbb', '2026-10-13T09:00:00+07:00', cost={'total_usd': 0.02})
    seed(client, '20261013T100000_cccccccc', '2026-10-13T10:00:00+07:00', cost={'total_usd': 0.04})

    by_day = await (await client.get('/api/stats')).json()
    by_month = await (await client.get('/api/stats', params={'by': 'month'})).json()

    assert by_day == {'by': 'day', 'rows': [
        {'period': '2026-10-13', 'total': 2, 'urgent': 0, 'spam': 0, 'total_usd': 0.06},
        {'period': '2026-10-12', 'total': 1, 'urgent': 1, 'spam': 0, 'total_usd': 0.01},
    ]}
    assert by_month['rows'] == [{'period': '2026-10', 'total': 3, 'urgent': 1, 'spam': 0, 'total_usd': 0.07}]
    assert (await client.get('/api/stats', params={'by': 'year'})).status == 400


# --- settings ----------------------------------------------------------------


def basics(settings):
    return {key: settings[key] for key in ('owner_name', 'greeting', 'system_doc', 'blocked_numbers')}


async def test_settings_start_from_the_defaults(client):
    settings = await (await client.get('/api/settings')).json()

    assert basics(settings) == {
        'owner_name': 'chủ máy', 'greeting': DEFAULT_GREETING, 'system_doc': 'doc', 'blocked_numbers': [],
    }
    assert (settings['status'], settings['language'], settings['voice_id'], settings['work_hours']) == (
        'normal', 'vi', 'v1', None,
    )


async def test_saved_settings_are_returned_and_blank_restores_the_default(client):
    saved = await client.put('/api/settings', json={'owner_name': '  anh Tuấn ', 'greeting': 'Alo, Tuấn nghe.'})
    assert saved.status == 200
    assert basics(await saved.json()) == {
        'owner_name': 'anh Tuấn', 'greeting': 'Alo, Tuấn nghe.', 'system_doc': 'doc', 'blocked_numbers': [],
    }

    reset = await (await client.put('/api/settings', json={'greeting': '', 'owner_name': None})).json()
    assert basics(reset) == {
        'owner_name': 'chủ máy', 'greeting': DEFAULT_GREETING, 'system_doc': 'doc', 'blocked_numbers': [],
    }


async def test_a_working_schedule_round_trips_through_the_api(client):
    hours = {'days': [1, 2, 3, 4, 5], 'from': '08:00', 'to': '17:30'}

    saved = await (await client.put('/api/settings', json={'work_hours': hours})).json()

    assert saved['work_hours'] == hours
    assert (await client.put('/api/settings', json={'work_hours': {'days': [9], 'from': '08:00', 'to': '17:30'}})).status == 400


async def test_the_default_greeting_follows_the_owner_name(client):
    settings = await (await client.put('/api/settings', json={'owner_name': 'chị Lan'})).json()

    assert settings['greeting'] == 'Chào bạn, chị Lan hiện không thể nghe máy. Mình là trợ lý AI, cuộc gọi này được ghi âm để chuyển lời nhắn. Bạn cần nhắn gì không ạ?'


@pytest.mark.parametrize('body', [
    {'greeting': 'x' * 501},
    {'system_doc': 'x' * 8001},
    {'owner_name': 'x' * 81},
    {'greeting': 123},
    {'voice': 'someone'},
    ['greeting'],
])
async def test_settings_reject_oversized_mistyped_or_unknown_values(client, body):
    assert (await client.put('/api/settings', json=body)).status == 400
    assert (await (await client.get('/api/settings')).json())['greeting'] == DEFAULT_GREETING


async def test_the_next_call_uses_the_saved_greeting_and_persona(tmp_path):
    client, upstream = await start_app(tmp_path, FakeUpstream(reply='Dạ.'))
    try:
        await client.put('/api/settings', json={
            'owner_name': 'chị Lan', 'greeting': 'Alo, Lan đang bận.', 'system_doc': 'Bạn nghe máy thay {{OWNER_NAME}}.',
        })

        ws = await client.ws_connect('/ws')
        greeting_tts = await upstream.tts_socket(0)
        # The owner's own words say neither "AI" nor "ghi âm": the disclosure goes first.
        assert greeting_tts.sent[1:5] == [
            {'text': 'Mình là trợ lý AI, '}, {'text': 'cuộc gọi này được ghi âm để chuyển lời nhắn. '},
            {'text': 'Alo, '}, {'text': 'Lan đang bận. '},
        ]

        greeting_tts.feed({'isFinal': True})
        upstream.stt.feed({'type': 'conversation.item.input_audio_transcription.completed', 'transcript': 'Alo'})
        await upstream.tts_socket(1)

        system_prompt = upstream.openai.calls[0]['messages'][0]['content']
        assert 'Bạn nghe máy thay chị Lan.' in system_prompt
        assert '[HANGUP]' in system_prompt, 'the hangup rule survives whatever the owner writes'
        await ws.close()
    finally:
        await client.close()


# --- blocked numbers ---------------------------------------------------------


async def test_blocked_numbers_are_stored_as_bare_national_numbers_without_duplicates(client):
    saved = await client.put('/api/settings', json={
        'blocked_numbers': ['+84 901 234 567', '028-7654-321', '0901234567', '   '],
    })

    assert (await saved.json())['blocked_numbers'] == ['0901234567', '0287654321']
    assert (await (await client.put('/api/settings', json={'blocked_numbers': None})).json())['blocked_numbers'] == []


@pytest.mark.parametrize('value', ['0901234567', [901234567], ['1' * 21], ['0900000000'] * 501, {'a': 1}])
async def test_blocked_numbers_must_be_a_short_list_of_phone_numbers(client, value):
    assert (await client.put('/api/settings', json={'blocked_numbers': value})).status == 400


async def test_a_blocked_caller_is_turned_away_and_the_attempt_is_filed_as_read_spam(client):
    await client.put('/api/settings', json={'blocked_numbers': ['0901234567']})

    answer = await client.post('/api/blocked', json={'caller': '+84901234567'})

    assert await answer.json() == {'blocked': True}
    [row] = client.app[DB].list_calls()
    assert (row['caller'], row['category'], row['read']) == ('0901234567', 'spam', True)
    assert 'chặn' in row['summary']


async def test_other_callers_are_let_through_and_leave_no_trace(client):
    await client.put('/api/settings', json={'blocked_numbers': ['0901234567']})

    assert await (await client.post('/api/blocked', json={'caller': '0912345678'})).json() == {'blocked': False}
    assert await (await client.post('/api/blocked', json={'caller': None})).json() == {'blocked': False}
    assert client.app[DB].list_calls() == []


async def test_the_block_check_needs_a_json_object(client):
    assert (await client.post('/api/blocked', data=b'nope')).status == 400
    assert (await client.post('/api/blocked', json=['0901234567'])).status == 400


# --- schedule, voice and language apply to the next call ----------------------


async def test_while_in_a_meeting_callers_hear_the_meeting_greeting(tmp_path):
    client, upstream = await start_app(tmp_path)
    try:
        await client.put('/api/settings', json={'status': 'meeting', 'meeting_greeting': 'Sếp đang họp.'})

        ws = await client.ws_connect('/ws')
        greeting_tts = await upstream.tts_socket(0)

        assert greeting_tts.sent[3] == {'text': 'Sếp đang họp. '}
        await ws.close()
    finally:
        await client.close()


async def test_the_chosen_voice_speaks_the_next_call(tmp_path):
    client, upstream = await start_app(tmp_path)
    try:
        await client.put('/api/settings', json={'voice_id': 'c38kUX8pkfYO2kHyqfFy'})

        ws = await client.ws_connect('/ws')
        await upstream.tts_socket(0)

        assert '/text-to-speech/c38kUX8pkfYO2kHyqfFy/stream-input' in upstream.tts_urls[0]
        await ws.close()
    finally:
        await client.close()


async def test_auto_language_lets_stt_detect_and_tells_the_llm_to_follow_the_caller(tmp_path):
    client, upstream = await start_app(tmp_path, FakeUpstream(reply='Sure.'))
    try:
        await client.put('/api/settings', json={'language': 'auto'})

        ws = await client.ws_connect('/ws')
        greeting_tts = await upstream.tts_socket(0)
        transcription = upstream.stt.sent[0]['session']['audio']['input']['transcription']
        assert 'language' not in transcription

        greeting_tts.feed({'isFinal': True})
        upstream.stt.feed({'type': 'conversation.item.input_audio_transcription.completed', 'transcript': 'Hello'})
        await upstream.tts_socket(1)
        assert 'ngôn ngữ người gọi' in upstream.openai.calls[0]['messages'][0]['content']
        await ws.close()
    finally:
        await client.close()


# --- voices ------------------------------------------------------------------

VOICES_REPLY = {
    'voices': [
        {'voice_id': 'abc123', 'name': 'Mai', 'category': 'premade', 'labels': {'gender': 'female', 'accent': 'northern'},
         'preview_url': 'https://storage.example/mai.mp3', 'settings': {'ignored': True}},
        {'voice_id': 'def456', 'name': 'Giọng của tôi', 'category': 'cloned', 'labels': None, 'preview_url': None},
    ],
    'has_more': False,
}


async def test_voices_are_listed_from_elevenlabs_with_the_current_choice(tmp_path):
    http = FakeHttp(replies={'/v2/voices': (200, VOICES_REPLY)})
    client, _ = await start_app(tmp_path, http=http)
    try:
        listing = await (await client.get('/api/voices')).json()

        assert listing == {
            'current': 'v1',
            'voices': [
                {'voice_id': 'abc123', 'name': 'Mai', 'category': 'premade',
                 'labels': {'gender': 'female', 'accent': 'northern'}, 'preview_url': 'https://storage.example/mai.mp3'},
                {'voice_id': 'def456', 'name': 'Giọng của tôi', 'category': 'cloned', 'labels': {}, 'preview_url': None},
            ],
        }
        request = http.requests[0]
        assert request['url'] == 'https://api.elevenlabs.io/v2/voices'
        assert request['headers'] == {'xi-api-key': 'k2'}
    finally:
        await client.close()


async def test_a_failing_voice_listing_is_a_bad_gateway_that_says_why(tmp_path):
    missing_permission = {'detail': {'message': 'The API key you used is missing the permission voices_read.'}}
    for http, reason in (
        (FakeHttp(replies={'/v2/voices': (401, missing_permission)}), 'missing the permission voices_read'),
        (FakeHttp(replies={'/v2/voices': (500, {'detail': 'boom'})}), 'boom'),
        (FakeHttp(error=OSError('offline')), 'không kết nối được'),
    ):
        client, _ = await start_app(tmp_path, http=http)
        try:
            response = await client.get('/api/voices')
            assert response.status == 502
            assert reason in await response.text()
        finally:
            await client.close()


def clone_form(name='Giọng của tôi', samples=(b'RIFF-sample-1', b'RIFF-sample-2')):
    form = FormData()
    if name is not None:
        form.add_field('name', name)
    for index, sample in enumerate(samples):
        form.add_field('files', io.BytesIO(sample), filename=f'mau-{index}.wav', content_type='audio/wav')
    return form


async def test_cloning_a_voice_uploads_the_samples_and_selects_the_new_voice(tmp_path):
    http = FakeHttp(replies={'/v1/voices/add': (200, {'voice_id': 'newVoice123', 'requires_verification': False})})
    client, _ = await start_app(tmp_path, http=http)
    try:
        response = await client.post('/api/voices/clone', data=clone_form())

        assert response.status == 200
        assert await response.json() == {'voice_id': 'newVoice123'}
        assert (await (await client.get('/api/settings')).json())['voice_id'] == 'newVoice123'

        request = http.requests[0]
        assert (request['method'], request['url']) == ('POST', 'https://api.elevenlabs.io/v1/voices/add')
        assert request['headers'] == {'xi-api-key': 'k2'}
        fields = request['data']._fields  # (disposition, headers, value) per form field
        assert [(field[0]['name'], field[0].get('filename')) for field in fields] == [
            ('files', 'mau-0.wav'), ('files', 'mau-1.wav'), ('name', None),
        ]
        assert [field[2].getvalue() for field in fields[:2]] == [b'RIFF-sample-1', b'RIFF-sample-2']
        assert fields[2][2] == 'Giọng của tôi'
    finally:
        await client.close()


async def test_cloning_needs_a_name_and_at_least_one_sample(tmp_path):
    http = FakeHttp()
    client, _ = await start_app(tmp_path, http=http)
    try:
        assert (await client.post('/api/voices/clone', data=clone_form(name=None))).status == 400
        assert (await client.post('/api/voices/clone', data=clone_form(samples=()))).status == 400
        assert (await client.post('/api/voices/clone', json={'name': 'x'})).status == 400
        assert http.requests == [], 'nothing reaches ElevenLabs'
    finally:
        await client.close()


async def test_cloning_refuses_oversized_uploads(tmp_path):
    http = FakeHttp()
    client, _ = await start_app(tmp_path, http=http)
    try:
        too_big = clone_form(samples=(bytes(11 * 1024 * 1024),))

        assert (await client.post('/api/voices/clone', data=too_big)).status == 413
        assert http.requests == []
    finally:
        await client.close()


async def test_a_refused_clone_reports_why_and_keeps_the_current_voice(tmp_path):
    http = FakeHttp(replies={'/v1/voices/add': (401, {'detail': {'message': 'Your plan does not include voice cloning.'}})})
    client, _ = await start_app(tmp_path, http=http)
    try:
        response = await client.post('/api/voices/clone', data=clone_form())

        assert response.status == 502
        assert 'does not include voice cloning' in await response.text()
        assert (await (await client.get('/api/settings')).json())['voice_id'] == 'v1'
    finally:
        await client.close()
