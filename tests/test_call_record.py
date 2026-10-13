import wave
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fakes import FakeOpenai
from voice_agent.call_record import CallRecord, compute_cost, purge_old_calls
from voice_agent.db import Database

PRICING = {
    'llm': {'input_per_1m_tokens': 0.40, 'output_per_1m_tokens': 1.60},
    'stt': {
        'audio_input_per_1m_tokens': 6.00, 'text_input_per_1m_tokens': 2.50,
        'output_per_1m_tokens': 10.00, 'per_minute': 0.006,
    },
    'tts': {'per_1k_characters': 0.04},
    'usd_to_vnd': 25000,
}
VIETNAM = timezone(timedelta(hours=7))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'voicemail.db')
    yield database
    database.close()


def make_record(tmp_path, db, session_id='abc12345'):
    return CallRecord(session_id=session_id, data_dir=tmp_path, pricing=PRICING, db=db, tz=VIETNAM)


def with_message(record):
    record.set_caller('{"type": "callMeta", "caller": "0901234567"}')
    record.add_transcript('assistant', 'Chào bạn, bạn cần nhắn gì không ạ?')
    record.add_transcript('user', 'Mình là Nam, nhờ anh Tuấn gọi lại gấp về hợp đồng.')
    return record


def test_caller_audio_is_saved_as_a_playable_wav(tmp_path, db):
    record = make_record(tmp_path, db)
    record.write_audio(bytes([1, 0, 2, 0]))
    record.write_audio(bytes([3, 0]))

    record.close()

    with wave.open(str(record.dir / 'caller.wav'), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        assert wav.readframes(wav.getnframes()) == bytes([1, 0, 2, 0, 3, 0])


def test_close_saves_the_call_before_any_network_call_is_made(tmp_path, db):
    record = with_message(make_record(tmp_path, db))

    record.close()

    call = db.get_call(record.id)
    assert record.dir == tmp_path / 'calls' / record.id
    assert call['caller'] == '0901234567'
    assert call['read'] is False
    assert call['summary'] is None
    assert [(t['role'], t['text']) for t in call['transcript']] == [
        ('assistant', 'Chào bạn, bạn cần nhắn gì không ạ?'),
        ('user', 'Mình là Nam, nhờ anh Tuấn gọi lại gấp về hợp đồng.'),
    ]
    assert call['started_at'] <= call['ended_at']
    assert call['duration_seconds'] >= 0


def test_times_are_recorded_in_the_configured_offset_whatever_the_servers_clock_zone(tmp_path, db):
    record = make_record(tmp_path, db)
    record.close()

    call = db.get_call(record.id)

    assert call['started_at'].endswith('+07:00')
    assert call['ended_at'].endswith('+07:00')
    assert record.id.startswith(datetime.now(VIETNAM).strftime('%Y%m%d'))


@pytest.mark.parametrize('raw, caller', [
    ('{"type": "callMeta", "caller": "+84 90-123 4567"}', '+84901234567'),
    ('{"type": "callMeta", "caller": "0901234567<script>alert(1)</script>"}', '09012345671'),
    ('{"type": "callMeta", "caller": "' + '9' * 50 + '"}', '9' * 20),
    ('{"type": "callMeta"}', None),
    ('{"type": "somethingElse", "caller": "0901234567"}', None),
    ('["not", "an", "object"]', None),
    ('not json at all', None),
])
def test_set_caller_only_trusts_a_well_formed_call_meta_frame(tmp_path, db, raw, caller):
    record = make_record(tmp_path, db)
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


def test_usage_is_accumulated_from_llm_and_stt_reports(tmp_path, db):
    record = make_record(tmp_path, db)
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


async def test_summarize_fills_in_the_summary_and_prices_the_call(tmp_path, db):
    record = with_message(make_record(tmp_path, db))
    record.add_llm_usage(SimpleNamespace(prompt_tokens=1000, completion_tokens=100))
    record.usage['tts_characters'] = 500
    record.close()
    openai = FakeOpenai()

    await record.summarize(openai)

    dialogue = openai.calls[0]['messages'][-1]['content']
    assert 'Người gọi: Mình là Nam, nhờ anh Tuấn gọi lại gấp về hợp đồng.' in dialogue
    assert openai.calls[0]['response_format'] == {'type': 'json_object'}

    call = db.get_call(record.id)
    assert (call['caller_name'], call['summary'], call['urgency']) == ('Anh Nam', 'Nhờ gọi lại về hợp đồng.', 'cao')
    assert call['category'] == 'bình thường'
    # conversation (1000 in / 100 out) + the summary call itself (200 in / 40 out)
    assert call['usage']['llm_input_tokens'] == 1200
    assert call['usage']['llm_output_tokens'] == 140
    assert call['total_usd'] == pytest.approx(1200 * 0.40 / 1e6 + 140 * 1.60 / 1e6 + 0.5 * 0.04)
    assert call['cost']['total_usd'] == call['total_usd']


async def test_a_sales_call_is_filed_as_spam(tmp_path, db):
    record = with_message(make_record(tmp_path, db))
    record.close()
    summary = {'caller_name': 'Không rõ', 'summary': 'Mời mua bảo hiểm.', 'urgency': 'thấp', 'category': 'spam'}

    await record.summarize(FakeOpenai(summary=summary))

    assert db.get_call(record.id)['category'] == 'spam'


@pytest.mark.parametrize('category', ['Spam!', 'quảng cáo', None, 42])
async def test_any_category_the_model_invents_counts_as_a_normal_call(tmp_path, db, category):
    record = with_message(make_record(tmp_path, db))
    record.close()
    summary = {'caller_name': 'Nam', 'summary': 'Nhờ gọi lại.', 'urgency': 'thấp', 'category': category}

    await record.summarize(FakeOpenai(summary=summary))

    assert db.get_call(record.id)['category'] == 'bình thường'


async def test_a_call_with_no_message_skips_the_llm(tmp_path, db):
    record = make_record(tmp_path, db)
    record.add_transcript('assistant', 'Chào bạn, bạn cần nhắn gì không ạ?')
    record.close()
    openai = FakeOpenai()

    await record.summarize(openai)

    call = db.get_call(record.id)
    assert openai.calls == []
    assert 'không để lại lời nhắn' in call['summary']
    assert call['category'] == 'bình thường'


async def test_when_the_summary_llm_fails_the_call_is_kept_with_the_callers_own_words(tmp_path, db):
    record = with_message(make_record(tmp_path, db))
    record.close()

    await record.summarize(FakeOpenai(summary_error=RuntimeError('boom')))

    call = db.get_call(record.id)
    assert len(call['transcript']) == 2
    assert 'nhờ anh Tuấn gọi lại gấp' in call['summary']
    assert call['total_usd'] is not None, 'the call is still priced'


def test_purge_removes_the_row_and_the_recording_of_calls_past_retention(tmp_path, db):
    old = make_record(tmp_path, db, session_id='old00000')
    old.close()
    recent = make_record(tmp_path, db, session_id='new00000')
    recent.close()
    # Age the first call by rewriting its start time, as if it were recorded 40 days ago.
    aged = db.get_call(old.id)
    aged['started_at'] = (datetime.now(VIETNAM) - timedelta(days=40)).isoformat()
    db.save_call(aged)

    purged = purge_old_calls(db, tmp_path, retention_days=30, tz=VIETNAM)

    assert purged == [old.id]
    assert db.get_call(old.id) is None and not old.dir.exists()
    assert db.get_call(recent.id) is not None and (recent.dir / 'caller.wav').exists()
