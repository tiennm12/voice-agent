import asyncio

from fakes import FakeHttp, FakeTts
from voice_agent.script import CachedTts, off_script, script_instructions, script_replies

LINES = ['Tên bạn là gì?', 'Có gấp không?', 'Số gọi lại?', 'Cảm ơn, chào bạn.']
AUDIO = b'\x01\x02\x03\x04'


async def next_line(turns, llm_stream=None):
    """turns: (caller, agent) pairs, the last caller line without an answer yet."""
    messages = [{'role': 'system', 'content': 'x'}, {'role': 'assistant', 'content': 'Chào bạn.'}]
    for caller, agent in turns:
        messages.append({'role': 'user', 'content': caller})
        if agent:
            messages.append({'role': 'assistant', 'content': agent})
    return ''.join([token async for token in script_replies(LINES, llm_stream)(messages)])


def chat(reply):
    async def stream(messages):
        assert messages[-1]['role'] == 'user'
        for word in reply.split(' '):
            yield word + ' '
    return stream


async def test_each_answer_gets_the_next_line_not_said_yet_and_the_last_line_hangs_up():
    assert await next_line([('Nhờ anh gọi lại.', None)]) == 'Tên bạn là gì?'
    assert await next_line([('Nhờ anh gọi lại.', 'Tên bạn là gì?'), ('Nam.', None)]) == 'Có gấp không?'
    turns = [('a', 'Tên bạn là gì?'), ('b', 'Có gấp không?'), ('c', 'Số gọi lại?'), ('d', None)]
    assert await next_line(turns) == 'Cảm ơn, chào bạn.[HANGUP]'


async def test_a_caller_who_says_goodbye_skips_the_remaining_questions():
    assert await next_line([('Không có gì đâu, tạm biệt.', None)]) == 'Cảm ơn, chào bạn.[HANGUP]'


async def test_a_question_goes_to_the_chat_model_then_the_script_carries_on_where_it_was():
    reply = await next_line([('Mình là Nam.', 'Tên bạn là gì?'), ('Anh Tuấn đi đâu rồi?', None)], chat('Mình không rõ ạ.'))
    assert reply == 'Mình không rõ ạ.  Có gấp không?'
    assert await next_line([('Anh Tuấn đâu?', None)]) == 'Tên bạn là gì?', 'no chat model: the script just goes on'


async def test_a_noise_that_cut_off_the_answer_does_not_hide_the_question():
    reply = await next_line([('Anh Tuấn đi đâu rồi?', None), ('Mà', None)], chat('Mình không rõ ạ.'))
    assert reply.startswith('Mình không rõ ạ.')


async def test_a_chat_model_answer_that_hangs_up_is_not_followed_by_a_question():
    reply = await next_line([('Có phải số anh Tuấn không?', None)], chat('Đúng rồi, chào bạn. [HANGUP]'))
    assert reply.strip() == 'Đúng rồi, chào bạn. [HANGUP]'


def test_questions_are_told_apart_from_answers():
    for text in ('Anh Tuấn đi đâu rồi?', 'bao giờ anh ấy về', 'Ai đang nghe máy vậy', 'Gọi lại được không'):
        assert off_script(text), text
    for text in ('Mình là Nam.', 'Gấp lắm.', 'Số 0901234567.', 'Nhờ anh mai gọi lại, hai giờ chiều nhé.'):
        assert not off_script(text), text


def test_the_realtime_instructions_list_every_question_and_the_closing_line():
    text = script_instructions(LINES)
    assert '1. Tên bạn là gì?' in text and '3. Số gọi lại?' in text
    assert text.endswith('Cảm ơn, chào bạn.')


def make_tts(tmp_path, http, **kwargs):
    tts = CachedTts(**{
        'http': http, 'cache_dir': tmp_path, 'api_key': 'k', 'voice_id': 'v1', 'model_id': 'm', 'audio': 'pcm',
        **kwargs,
    })
    played = []
    tts.on('audio_chunk', played.append)
    tts.on('turn_complete', lambda: played.append('done'))
    return tts, played


async def speak(tts, *chunks):
    await tts.connect()
    for chunk in chunks:
        await tts.send_text(chunk)
    await tts.end_turn()


async def test_a_line_is_rendered_once_and_then_played_from_disk(tmp_path):
    http = FakeHttp(replies={'text-to-speech': (200, AUDIO)})
    first, played = make_tts(tmp_path, http)
    await speak(first, 'Cảm ơn bạn,', 'chào nhé.')  # the chunker's clauses make one request for the whole line

    assert played == [AUDIO, 'done']
    assert len(http.requests) == 1
    request = http.requests[0]
    assert request['url'] == 'https://api.elevenlabs.io/v1/text-to-speech/v1?output_format=pcm_24000'
    assert request['json']['text'] == 'Cảm ơn bạn, chào nhé.'
    assert first.characters_sent == len('Cảm ơn bạn, chào nhé.')

    second, played = make_tts(tmp_path, http)  # the next call
    await speak(second, 'Cảm ơn bạn, chào nhé.')
    assert played == [AUDIO, 'done']
    assert len(http.requests) == 1, 'played from disk'
    assert second.characters_sent == 0


async def test_another_voice_or_format_is_rendered_separately(tmp_path):
    http = FakeHttp(replies={'text-to-speech': (200, AUDIO)})
    for kwargs in ({}, {'voice_id': 'v2'}, {'audio': 'mp3'}):
        tts, _ = make_tts(tmp_path, http, **kwargs)
        await speak(tts, 'Alo.')
    assert len(http.requests) == 3


async def test_prefetch_renders_ahead_so_the_turn_does_not_wait(tmp_path):
    http = FakeHttp(replies={'text-to-speech': (200, AUDIO)})
    tts, played = make_tts(tmp_path, http)
    tts.prefetch(['Một.', 'Hai.'])
    await speak(tts, 'Một.')
    await speak(tts, 'Hai.')
    assert len(http.requests) == 2, 'each line fetched once, prefetch and turn sharing it'
    assert len(list(tmp_path.glob('*.pcm'))) == 2


async def test_a_failed_render_raises_and_is_retried_on_the_next_turn(tmp_path):
    http = FakeHttp(replies={'text-to-speech': (401, None)})
    tts, played = make_tts(tmp_path, http)
    try:
        await speak(tts, 'Alo.')
    except RuntimeError as err:
        assert '401' in str(err)
    else:
        raise AssertionError('expected the render to fail')

    http.replies = {'text-to-speech': (200, AUDIO)}
    await speak(tts, 'Alo.')
    assert played == [AUDIO, 'done']


# --- with a live fallback ------------------------------------------------------

def make_live_tts(tmp_path, live):
    http = FakeHttp(replies={'text-to-speech': (200, AUDIO)})
    tts, played = make_tts(tmp_path, http, live=live, known=['Cảm ơn bạn, chào nhé.', 'Alo.'])
    return tts, played, http


async def test_a_known_line_plays_from_the_cache_without_waiting_for_elevenlabs(tmp_path):
    live = FakeTts(deferred=True)  # its handshake never finishes
    tts, played, _ = make_live_tts(tmp_path, live)
    async with asyncio.timeout(1):
        await speak(tts, 'Cảm ơn bạn,', 'chào nhé.')
    assert played == [AUDIO, 'done']
    assert live.sent_text == [] and live.aborts == 1


async def test_text_off_the_script_goes_live_from_its_first_chunk(tmp_path):
    live = FakeTts()
    tts, played, http = make_live_tts(tmp_path, live)
    await speak(tts, 'Anh Tuấn đang bận,', 'bạn nhắn gì ạ?')
    assert live.sent_text == ['Anh Tuấn đang bận,', 'bạn nhắn gì ạ?'] and live.end_turns == 1
    assert http.requests == [], 'nothing rendered'
    live.emit('audio_chunk', b'live')
    live.emit('turn_complete')
    assert played == [b'live', 'done'], "the live client's audio is relayed"


async def test_a_line_that_starts_like_a_known_one_then_differs_goes_live_with_what_came_before(tmp_path):
    live = FakeTts()
    tts, _, _ = make_live_tts(tmp_path, live)
    await speak(tts, 'Cảm ơn bạn,', 'hẹn gặp lại.')
    assert live.sent_text == ['Cảm ơn bạn, hẹn gặp lại.'] and live.end_turns == 1
    await speak(tts, 'Cảm ơn bạn,')  # a prefix to the very end: still not a known line
    assert live.sent_text[-1] == 'Cảm ơn bạn,' and live.end_turns == 2


async def test_characters_count_both_rendered_lines_and_live_text(tmp_path):
    live = FakeTts()
    live.characters_sent = 7
    tts, _, _ = make_live_tts(tmp_path, live)
    await speak(tts, 'Alo.')
    assert tts.characters_sent == len('Alo.') + 7
