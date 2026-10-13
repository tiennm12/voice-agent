import json
import wave
from collections import Counter
from types import SimpleNamespace

import pytest

from fakes import FakeHttp, FakeOpenai
from voice_agent.call_record import CallRecord, compute_cost

PRICING = {
    'llm': {'input_per_1m_tokens': 0.40, 'output_per_1m_tokens': 1.60},
    'stt': {
        'audio_input_per_1m_tokens': 6.00, 'text_input_per_1m_tokens': 2.50,
        'output_per_1m_tokens': 10.00, 'per_minute': 0.006,
    },
    'tts': {'per_1k_characters': 0.04},
    'usd_to_vnd': 25000,
}
TELEGRAM = {'telegram_bot_token': 'TOKEN', 'telegram_chat_id': '42'}


def make_record(tmp_path, pricing=PRICING):
    return CallRecord(session_id='abc12345', data_dir=tmp_path, pricing=pricing)


def saved(record):
    return json.loads((record.dir / 'call.json').read_text(encoding='utf-8'))


def with_message(record):
    record.set_caller('{"type": "callMeta", "caller": "0901234567"}')
    record.add_transcript('assistant', 'Chào bạn, bạn cần nhắn gì không ạ?')
    record.add_transcript('user', 'Mình là Nam, nhờ anh Tuấn gọi lại gấp về hợp đồng.')
    return record


def test_caller_audio_is_saved_as_a_playable_wav(tmp_path):
    record = make_record(tmp_path)
    record.write_audio(bytes([1, 0, 2, 0]))
    record.write_audio(bytes([3, 0]))

    record.close()

    with wave.open(str(record.dir / 'caller.wav'), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        assert wav.readframes(wav.getnframes()) == bytes([1, 0, 2, 0, 3, 0])


def test_close_saves_the_call_before_any_network_call_is_made(tmp_path):
    record = with_message(make_record(tmp_path))

    record.close()

    call = saved(record)
    assert record.dir.parent == tmp_path / 'calls'
    assert call['id'] == record.dir.name
    assert call['caller'] == '0901234567'
    assert call['read'] is False
    assert call['summary'] is None
    assert [(t['role'], t['text']) for t in call['transcript']] == [
        ('assistant', 'Chào bạn, bạn cần nhắn gì không ạ?'),
        ('user', 'Mình là Nam, nhờ anh Tuấn gọi lại gấp về hợp đồng.'),
    ]
    assert call['started_at'] <= call['ended_at']


@pytest.mark.parametrize('raw, caller', [
    ('{"type": "callMeta", "caller": "+84 90-123 4567"}', '+84901234567'),
    ('{"type": "callMeta", "caller": "0901234567<script>alert(1)</script>"}', '09012345671'),
    ('{"type": "callMeta", "caller": "' + '9' * 50 + '"}', '9' * 20),
    ('{"type": "callMeta"}', None),
    ('{"type": "somethingElse", "caller": "0901234567"}', None),
    ('["not", "an", "object"]', None),
    ('not json at all', None),
])
def test_set_caller_only_trusts_a_well_formed_call_meta_frame(tmp_path, raw, caller):
    record = make_record(tmp_path)
    record.set_caller(raw)
    assert record.caller == caller


def test_compute_cost_prices_each_service_and_totals_them():
    usage = Counter(
        llm_input_tokens=1_000_000, llm_output_tokens=500_000,
        stt_audio_tokens=100_000, stt_text_tokens=200_000, stt_output_tokens=50_000, stt_seconds=120,
        tts_characters=2_000,
    )

    cost = compute_cost(usage, PRICING)

    assert cost['llm_usd'] == pytest.approx(0.40 + 0.80)
    assert cost['stt_usd'] == pytest.approx(0.60 + 0.50 + 0.50 + 0.012)
    assert cost['tts_usd'] == pytest.approx(0.08)
    assert cost['total_usd'] == pytest.approx(1.20 + 1.612 + 0.08)
    assert cost['total_vnd'] == round((1.20 + 1.612 + 0.08) * 25000)


def test_compute_cost_omits_vnd_without_an_exchange_rate():
    cost = compute_cost(Counter(tts_characters=1000), {**PRICING, 'usd_to_vnd': None})
    assert cost['total_usd'] == pytest.approx(0.04)
    assert 'total_vnd' not in cost


def test_usage_is_accumulated_from_llm_and_stt_reports(tmp_path):
    record = make_record(tmp_path)
    record.add_llm_usage(SimpleNamespace(prompt_tokens=100, completion_tokens=10))
    record.add_llm_usage(SimpleNamespace(prompt_tokens=150, completion_tokens=20))
    record.add_stt_usage({
        'type': 'tokens', 'input_tokens': 40, 'output_tokens': 12,
        'input_token_details': {'audio_tokens': 30, 'text_tokens': 10},
    })
    record.add_stt_usage({'type': 'tokens', 'input_tokens': 25, 'output_tokens': 5})  # no breakdown: all audio
    record.add_stt_usage({'type': 'duration', 'seconds': 7.5})

    assert record.usage == Counter(
        llm_input_tokens=250, llm_output_tokens=30,
        stt_audio_tokens=55, stt_text_tokens=10, stt_output_tokens=17, stt_seconds=7.5,
    )


async def test_summarizes_the_message_prices_the_call_and_notifies_telegram(tmp_path):
    record = with_message(make_record(tmp_path))
    record.add_llm_usage(SimpleNamespace(prompt_tokens=1000, completion_tokens=100))
    record.usage['tts_characters'] = 500
    record.close()
    openai, http = FakeOpenai(), FakeHttp()

    await record.summarize_and_notify(openai_client=openai, http=http, **TELEGRAM)

    dialogue = openai.calls[0]['messages'][-1]['content']
    assert 'Người gọi: Mình là Nam, nhờ anh Tuấn gọi lại gấp về hợp đồng.' in dialogue
    assert openai.calls[0]['response_format'] == {'type': 'json_object'}

    call = saved(record)
    assert call['summary'] == {'caller_name': 'Anh Nam', 'summary': 'Nhờ gọi lại về hợp đồng.', 'urgency': 'cao'}
    # conversation (1000 in / 100 out) + the summary call itself (200 in / 40 out)
    assert call['usage']['llm_input_tokens'] == 1200
    assert call['usage']['llm_output_tokens'] == 140
    assert call['cost']['total_usd'] == pytest.approx(1200 * 0.40 / 1e6 + 140 * 1.60 / 1e6 + 0.5 * 0.04)

    [(url, payload)] = http.posts
    assert url == 'https://api.telegram.org/botTOKEN/sendMessage'
    assert payload['chat_id'] == '42'
    for expected in ('0901234567', 'Anh Nam', 'Nhờ gọi lại về hợp đồng.', 'cao', f"${call['cost']['total_usd']:.4f}"):
        assert expected in payload['text'], expected


async def test_a_call_with_no_message_skips_the_llm_and_still_notifies(tmp_path):
    record = make_record(tmp_path)
    record.add_transcript('assistant', 'Chào bạn, bạn cần nhắn gì không ạ?')
    record.close()
    openai, http = FakeOpenai(), FakeHttp()

    await record.summarize_and_notify(openai_client=openai, http=http, **TELEGRAM)

    assert openai.calls == []
    assert 'không để lại lời nhắn' in saved(record)['summary']['summary']
    assert 'không để lại lời nhắn' in http.posts[0][1]['text']


async def test_when_the_summary_llm_fails_the_call_is_kept_and_the_callers_own_words_are_sent(tmp_path):
    record = with_message(make_record(tmp_path))
    record.close()
    openai, http = FakeOpenai(summary_error=RuntimeError('boom')), FakeHttp()

    await record.summarize_and_notify(openai_client=openai, http=http, **TELEGRAM)

    call = saved(record)
    assert len(call['transcript']) == 2
    assert 'nhờ anh Tuấn gọi lại gấp' in call['summary']['summary']
    assert 'nhờ anh Tuấn gọi lại gấp' in http.posts[0][1]['text']


async def test_without_telegram_settings_nothing_is_sent_but_the_call_is_still_saved(tmp_path):
    record = with_message(make_record(tmp_path))
    record.close()
    http = FakeHttp()

    await record.summarize_and_notify(
        openai_client=FakeOpenai(), http=http, telegram_bot_token=None, telegram_chat_id=None,
    )

    assert http.posts == []
    assert saved(record)['summary']['caller_name'] == 'Anh Nam'


async def test_a_telegram_network_error_is_logged_without_leaking_the_bot_token(tmp_path, caplog):
    record = with_message(make_record(tmp_path))
    record.close()
    http = FakeHttp(error=OSError('cannot reach https://api.telegram.org/botTOKEN/sendMessage'))

    await record.summarize_and_notify(openai_client=FakeOpenai(), http=http, **TELEGRAM)

    assert 'telegram notification failed' in caplog.text
    assert 'TOKEN' not in caplog.text
    assert saved(record)['summary']['caller_name'] == 'Anh Nam'


async def test_a_telegram_failure_does_not_lose_the_summary(tmp_path):
    record = with_message(make_record(tmp_path))
    record.close()

    await record.summarize_and_notify(openai_client=FakeOpenai(), http=FakeHttp(status=403), **TELEGRAM)

    assert saved(record)['summary']['caller_name'] == 'Anh Nam'
