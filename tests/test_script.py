from fakes import FakeHttp
from voice_agent.script import CachedTts, script_replies

LINES = ['Tên bạn là gì?', 'Có gấp không?', 'Số gọi lại?', 'Cảm ơn, chào bạn.']
AUDIO = b'\x01\x02\x03\x04'


async def next_line(*heard):
    messages = [{'role': 'system', 'content': 'x'}, {'role': 'assistant', 'content': 'Chào bạn.'}]
    for text in heard:
        messages.append({'role': 'user', 'content': text})
    return ''.join([token async for token in script_replies(LINES)(messages)])


async def test_each_answer_moves_one_line_down_and_the_last_line_hangs_up():
    assert await next_line('Nhờ anh gọi lại.') == 'Tên bạn là gì?'
    assert await next_line('Nhờ anh gọi lại.', 'Nam.') == 'Có gấp không?'
    assert await next_line('Nhờ anh gọi lại.', 'Nam.', 'Gấp.') == 'Số gọi lại?'
    assert await next_line('a', 'b', 'c', 'd') == 'Cảm ơn, chào bạn.[HANGUP]'
    assert await next_line('a', 'b', 'c', 'd', 'e') == 'Cảm ơn, chào bạn.[HANGUP]', 'talking over the goodbye: again'


async def test_a_caller_who_says_goodbye_skips_the_remaining_questions():
    assert await next_line('Không có gì đâu, tạm biệt.') == 'Cảm ơn, chào bạn.[HANGUP]'


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
