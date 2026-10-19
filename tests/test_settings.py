from datetime import datetime, timedelta, timezone

import pytest

from fakes import REQUIRED_ENV
from voice_agent.config import load_config
from voice_agent.settings import (
    DISCLOSURE, clean_setting, effective_settings, normalize_number, pick_greeting, with_disclosure,
)

VIETNAM = timezone(timedelta(hours=7))
CONFIG = load_config(env={**REQUIRED_ENV, 'OWNER_NAME': 'anh Tuấn'}, read_system_doc=lambda: 'doc')
OFFICE_HOURS = {'days': [1, 2, 3, 4, 5], 'from': '08:00', 'to': '17:30'}


def at(weekday_date, clock):
    return datetime.fromisoformat(f'{weekday_date}T{clock}:00').replace(tzinfo=VIETNAM)


MONDAY, SATURDAY = '2026-10-12', '2026-10-17'


def test_defaults_fill_every_setting_a_call_needs():
    settings = effective_settings(CONFIG, {})

    assert settings == {
        'owner_name': 'anh Tuấn',
        'greeting': 'Chào bạn, anh Tuấn hiện không thể nghe máy. Mình là trợ lý AI, cuộc gọi này được ghi âm để chuyển lời nhắn. Bạn cần nhắn gì không ạ?',
        'system_doc': 'doc',
        'script': 'Bạn cho mình xin tên với ạ?\nViệc này có gấp không ạ?\nBạn muốn anh Tuấn gọi lại số nào ạ?\n'
                  'Cảm ơn bạn, mình đã ghi lại lời nhắn và sẽ báo anh Tuấn ngay. Chào bạn nhé.',
        'blocked_numbers': [],
        'status': 'normal',
        'meeting_greeting': 'Chào bạn, anh Tuấn đang họp nên chưa nghe máy được. Mình là trợ lý AI, cuộc gọi này được ghi âm để chuyển lời nhắn. Bạn cần nhắn gì không ạ?',
        'work_hours': None,
        'after_hours_greeting': 'Chào bạn, hiện đã ngoài giờ làm việc của anh Tuấn. Mình là trợ lý AI, cuộc gọi này được ghi âm để chuyển lời nhắn. Bạn để lại lời nhắn nhé.',
        'voice_id': 'v1',
        'language': 'vi',
    }


def test_saved_settings_override_the_defaults_one_key_at_a_time():
    settings = effective_settings(CONFIG, {'greeting': 'Alo.', 'voice_id': 'abc', 'owner_name': 'chị Lan'})

    assert (settings['greeting'], settings['voice_id'], settings['system_doc']) == ('Alo.', 'abc', 'doc')
    assert 'chị Lan đang họp' in settings['meeting_greeting'], 'default greetings follow the saved owner name'


# --- which greeting a call gets ---------------------------------------------


def test_without_a_schedule_the_normal_greeting_is_used_at_any_hour():
    settings = effective_settings(CONFIG, {})

    assert pick_greeting(settings, at(SATURDAY, '03:00')) == settings['greeting']


@pytest.mark.parametrize('day, clock, expected', [
    (MONDAY, '08:00', 'greeting'),  # opening minute counts
    (MONDAY, '12:15', 'greeting'),
    (MONDAY, '17:29', 'greeting'),
    (MONDAY, '17:30', 'after_hours_greeting'),  # closing minute does not
    (MONDAY, '07:59', 'after_hours_greeting'),
    (MONDAY, '22:00', 'after_hours_greeting'),
    (SATURDAY, '10:00', 'after_hours_greeting'),  # not a working day
])
def test_outside_working_hours_callers_get_the_after_hours_greeting(day, clock, expected):
    settings = effective_settings(CONFIG, {'work_hours': OFFICE_HOURS})

    assert pick_greeting(settings, at(day, clock)) == settings[expected]


def test_in_a_meeting_wins_over_the_schedule():
    settings = effective_settings(CONFIG, {'work_hours': OFFICE_HOURS, 'status': 'meeting'})

    assert pick_greeting(settings, at(MONDAY, '10:00')) == settings['meeting_greeting']
    assert pick_greeting(settings, at(SATURDAY, '23:00')) == settings['meeting_greeting']


@pytest.mark.parametrize('key', ['greeting', 'meeting_greeting', 'after_hours_greeting'])
def test_a_greeting_that_hides_the_ai_or_the_recording_gets_the_disclosure_in_front(key):
    stored = {key: 'Alo, Lan nghe.', 'status': 'meeting' if key == 'meeting_greeting' else 'normal'}
    if key == 'after_hours_greeting':
        stored['work_hours'] = OFFICE_HOURS
    settings = effective_settings(CONFIG, stored)

    assert pick_greeting(settings, at(SATURDAY, '10:00')) == f'{DISCLOSURE} Alo, Lan nghe.'


def test_a_greeting_that_already_says_both_is_spoken_as_written():
    own = 'Alo, đây là trợ lý AI của Lan, cuộc gọi được ghi âm. Bạn nhắn gì ạ?'
    settings = effective_settings(CONFIG, {'greeting': own})

    assert pick_greeting(settings, at(MONDAY, '10:00')) == own
    # "AI" must be the word, not letters inside another one.
    assert with_disclosure('Alo, MAI đây, cuộc gọi được ghi âm.').startswith(DISCLOSURE)


# --- validation --------------------------------------------------------------


def test_text_settings_are_trimmed_and_blank_means_back_to_the_default():
    assert clean_setting('greeting', '  Alo.  ') == 'Alo.'
    assert clean_setting('after_hours_greeting', 'Hết giờ rồi.') == 'Hết giờ rồi.'
    for blank in ('', '   ', None):
        assert clean_setting('greeting', blank) is None


@pytest.mark.parametrize('key, value', [
    ('greeting', 'x' * 501),
    ('meeting_greeting', 'x' * 501),
    ('system_doc', 'x' * 8001),
    ('owner_name', 'x' * 81),
    ('greeting', 123),
    ('status', 'on holiday'),
    ('language', 'en'),
    ('voice_id', 'not a voice id!'),
    ('voice_id', 'x' * 65),
    ('nonsense', 'value'),
    ('nonsense', None),
])
def test_bad_settings_are_rejected(key, value):
    with pytest.raises(ValueError):
        clean_setting(key, value)


def test_choice_settings_accept_only_their_choices():
    assert clean_setting('status', 'meeting') == 'meeting'
    assert clean_setting('language', 'auto') == 'auto'
    assert clean_setting('voice_id', 'c38kUX8pkfYO2kHyqfFy') == 'c38kUX8pkfYO2kHyqfFy'
    assert clean_setting('status', None) is None


def test_work_hours_are_normalized():
    assert clean_setting('work_hours', {'days': [5, 1, 1, 3], 'from': '08:00', 'to': '17:30', 'extra': 1}) == {
        'days': [1, 3, 5], 'from': '08:00', 'to': '17:30',
    }
    assert clean_setting('work_hours', None) is None


@pytest.mark.parametrize('value', [
    {'days': [], 'from': '08:00', 'to': '17:30'},
    {'days': [0], 'from': '08:00', 'to': '17:30'},
    {'days': [8], 'from': '08:00', 'to': '17:30'},
    {'days': ['mon'], 'from': '08:00', 'to': '17:30'},
    {'days': [True], 'from': '08:00', 'to': '17:30'},
    {'days': [1], 'from': '8:00', 'to': '17:30'},
    {'days': [1], 'from': '08:00', 'to': '25:00'},
    {'days': [1], 'from': '17:30', 'to': '08:00'},  # overnight ranges are not supported
    {'days': [1], 'from': '08:00', 'to': '08:00'},
    {'days': [1], 'from': '08:00'},
    'nine to five',
    [1, 2, 3],
])
def test_malformed_work_hours_are_rejected(value):
    with pytest.raises(ValueError):
        clean_setting('work_hours', value)


def test_blocked_numbers_are_normalized_deduplicated_and_bounded():
    assert clean_setting('blocked_numbers', ['+84 901 234 567', '028-7654-321', '0901234567', '  ']) == [
        '0901234567', '0287654321',
    ]
    assert clean_setting('blocked_numbers', []) is None
    for bad in ('0901234567', [901234567], ['1' * 21], ['0900000000'] * 501):
        with pytest.raises(ValueError):
            clean_setting('blocked_numbers', bad)


def test_normalize_number_gives_one_spelling_per_vietnamese_number():
    assert normalize_number('+84 901 234 567') == '0901234567'
    assert normalize_number('84901234567') == '0901234567'
    assert normalize_number('0901.234.567') == '0901234567'
    assert normalize_number('8412') == '8412', 'too short to be +84 and a subscriber number'
