import pytest

from voice_agent.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'voicemail.db')
    yield database
    database.close()


def call(call_id, started_at, **fields):
    return {
        'id': call_id,
        'session_id': call_id[-8:],
        'caller': '0901234567',
        'started_at': started_at,
        'ended_at': started_at,
        'duration_seconds': 20,
        'transcript': [{'role': 'user', 'text': 'Alo', 'at': started_at}],
        'usage': {'tts_characters': 88},
        **fields,
    }


def test_a_saved_call_reads_back_with_its_structured_fields(db):
    db.save_call(call(
        'c1', '2026-10-13T16:45:51+07:00',
        caller_name='Nam', summary='Nhờ gọi lại.', urgency='cao', category='bình thường',
        cost={'total_usd': 0.0115, 'total_vnd': 303}, pbx_session_id='pbx-1',
    ))

    saved = db.get_call('c1')

    assert saved['caller'] == '0901234567'
    assert saved['pbx_session_id'] == 'pbx-1'
    assert saved['read'] is False
    assert (saved['caller_name'], saved['summary'], saved['urgency'], saved['category']) == (
        'Nam', 'Nhờ gọi lại.', 'cao', 'bình thường',
    )
    assert saved['transcript'] == [{'role': 'user', 'text': 'Alo', 'at': '2026-10-13T16:45:51+07:00'}]
    assert saved['usage'] == {'tts_characters': 88}
    assert saved['cost'] == {'total_usd': 0.0115, 'total_vnd': 303}
    assert saved['total_usd'] == 0.0115


def test_get_call_returns_none_for_an_unknown_id(db):
    assert db.get_call('nope') is None


def test_saving_a_call_again_updates_it_without_resetting_the_read_flag(db):
    db.save_call(call('c1', '2026-10-13T16:45:51+07:00'))
    db.mark_read('c1')

    db.save_call(call('c1', '2026-10-13T16:45:51+07:00', summary='Đã có tóm tắt.'))

    saved = db.get_call('c1')
    assert saved['summary'] == 'Đã có tóm tắt.'
    assert saved['read'] is True


def test_mark_read_toggles_and_reports_whether_the_call_exists(db):
    db.save_call(call('c1', '2026-10-13T16:45:51+07:00'))

    assert db.mark_read('c1') is True
    assert db.get_call('c1')['read'] is True
    assert db.mark_read('c1', read=False) is True
    assert db.get_call('c1')['read'] is False
    assert db.mark_read('nope') is False


def test_list_calls_is_newest_first_and_pages_with_before(db):
    for day in (10, 11, 12, 13):
        db.save_call(call(f'c{day}', f'2026-10-{day}T09:00:00+07:00'))

    first_page = db.list_calls(limit=2)
    assert [c['id'] for c in first_page] == ['c13', 'c12']
    assert 'transcript' not in first_page[0], 'the list stays light: no transcripts'

    second_page = db.list_calls(before=first_page[-1]['started_at'], limit=2)
    assert [c['id'] for c in second_page] == ['c11', 'c10']


def test_list_calls_filters_unread_urgent_and_spam(db):
    db.save_call(call('normal', '2026-10-13T09:00:00+07:00', urgency='thấp', category='bình thường'))
    db.save_call(call('urgent', '2026-10-13T10:00:00+07:00', urgency='cao', category='bình thường'))
    db.save_call(call('spam', '2026-10-13T11:00:00+07:00', urgency='thấp', category='spam'))
    db.mark_read('normal')

    assert [c['id'] for c in db.list_calls(only='unread')] == ['spam', 'urgent']
    assert [c['id'] for c in db.list_calls(only='urgent')] == ['urgent']
    assert [c['id'] for c in db.list_calls(only='spam')] == ['spam']
    with pytest.raises(ValueError):
        db.list_calls(only="unread'; DROP TABLE calls; --")


def test_stats_count_calls_and_cost_per_day_week_and_month(db):
    db.save_call(call('a', '2026-10-12T09:00:00+07:00', urgency='cao', cost={'total_usd': 0.01}))
    db.save_call(call('b', '2026-10-12T23:30:00+07:00', category='spam', cost={'total_usd': 0.02}))
    db.save_call(call('c', '2026-10-13T00:10:00+07:00', cost={'total_usd': 0.04}))
    db.save_call(call('d', '2026-09-30T12:00:00+07:00'))  # no cost yet

    by_day = db.stats('day')
    assert by_day[:2] == [
        {'period': '2026-10-13', 'total': 1, 'urgent': 0, 'spam': 0, 'total_usd': 0.04},
        {'period': '2026-10-12', 'total': 2, 'urgent': 1, 'spam': 1, 'total_usd': 0.03},
    ]
    assert [(row['period'], row['total']) for row in db.stats('month')] == [('2026-10', 3), ('2026-09', 1)]
    assert sum(row['total'] for row in db.stats('week')) == 4
    with pytest.raises(ValueError):
        db.stats('year')


def test_delete_older_than_removes_old_calls_and_returns_their_ids(db):
    db.save_call(call('old', '2026-09-01T09:00:00+07:00'))
    db.save_call(call('new', '2026-10-13T09:00:00+07:00'))

    deleted = db.delete_older_than('2026-10-01T00:00:00+07:00')

    assert deleted == ['old']
    assert db.get_call('old') is None
    assert db.get_call('new') is not None


def test_settings_round_trip_and_none_restores_the_default(db):
    assert db.get_settings() == {}

    db.set_settings({'greeting': 'Xin chào.', 'blocked_numbers': ['0900000000']})
    assert db.get_settings() == {'greeting': 'Xin chào.', 'blocked_numbers': ['0900000000']}

    db.set_settings({'greeting': None})
    assert db.get_settings() == {'blocked_numbers': ['0900000000']}


def test_data_survives_reopening_the_database_file(tmp_path):
    first = Database(tmp_path / 'voicemail.db')
    first.save_call(call('c1', '2026-10-13T16:45:51+07:00'))
    first.set_settings({'owner_name': 'anh Tuấn'})
    first.close()

    second = Database(tmp_path / 'voicemail.db')
    assert second.get_call('c1')['caller'] == '0901234567'
    assert second.get_settings() == {'owner_name': 'anh Tuấn'}
    second.close()
