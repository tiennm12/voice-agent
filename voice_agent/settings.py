"""The owner's settings: their defaults, their validation, and which greeting a given call gets."""
import re

# Text settings and their maximum length.
TEXT_LIMITS = {
    'owner_name': 80,
    'greeting': 500,
    'after_hours_greeting': 500,
    'meeting_greeting': 500,
    'system_doc': 8000,
}
# Settings that are one of a fixed set of values; the first is the default.
CHOICES = {
    'status': ('normal', 'meeting'),
    'language': ('vi', 'auto'),  # 'auto': answer in whatever language the caller speaks
}
VOICE_ID = re.compile(r'[A-Za-z0-9]{1,64}')
CLOCK = re.compile(r'([01]\d|2[0-3]):[0-5]\d')
MAX_BLOCKED_NUMBERS = 500
MAX_NUMBER_DIGITS = 20
# Every caller hears this, or what it says, first: the AI Law (134/2025/QH15, art. 11) requires telling
# people they are talking to an AI, the Personal Data Protection Law (91/2025/QH15) that they are recorded.
DISCLOSURE = 'Mình là trợ lý AI, cuộc gọi này được ghi âm để chuyển lời nhắn.'


def effective_settings(config, stored):
    """What a call actually uses: the owner's saved settings, with the defaults filling the gaps."""
    owner = stored.get('owner_name') or config.owner_name
    return {
        'owner_name': owner,
        'greeting': stored.get('greeting')
        or f'Chào bạn, {owner} hiện không thể nghe máy. {DISCLOSURE} Bạn cần nhắn gì không ạ?',
        'system_doc': stored.get('system_doc') or config.system_doc,
        'blocked_numbers': stored.get('blocked_numbers') or [],
        'status': stored.get('status') or CHOICES['status'][0],
        'meeting_greeting': stored.get('meeting_greeting')
        or f'Chào bạn, {owner} đang họp nên chưa nghe máy được. {DISCLOSURE} Bạn cần nhắn gì không ạ?',
        # {'days': [1..7, Monday = 1], 'from': 'HH:MM', 'to': 'HH:MM'}, or None for "no schedule".
        'work_hours': stored.get('work_hours'),
        'after_hours_greeting': stored.get('after_hours_greeting')
        or f'Chào bạn, hiện đã ngoài giờ làm việc của {owner}. {DISCLOSURE} Bạn để lại lời nhắn nhé.',
        'voice_id': stored.get('voice_id') or config.elevenlabs_voice_id,
        'language': stored.get('language') or CHOICES['language'][0],
    }


def pick_greeting(settings, now):
    """The greeting for a call arriving at `now` (already in the owner's time zone), disclosure included."""
    if settings['status'] == 'meeting':
        return with_disclosure(settings['meeting_greeting'])
    hours = settings['work_hours']
    # Zero-padded HH:MM strings compare like times.
    if hours and not (now.isoweekday() in hours['days'] and hours['from'] <= f'{now:%H:%M}' < hours['to']):
        return with_disclosure(settings['after_hours_greeting'])
    return with_disclosure(settings['greeting'])


def with_disclosure(greeting):
    """The greeting as spoken: one the owner wrote without saying "AI" and "ghi âm" gets DISCLOSURE in front."""
    if re.search(r'\bAI\b', greeting) and 'ghi âm' in greeting.lower():
        return greeting
    return f'{DISCLOSURE} {greeting}'


def normalize_number(text):
    """One spelling per phone number, so '+84 901 234 567' and '0901234567' compare equal.

    ponytail: knows Vietnam's +84 prefix only. Use a phone-number library if callers ever come from elsewhere.
    """
    digits = re.sub(r'\D', '', text)
    if digits.startswith('84') and len(digits) >= 11:
        digits = '0' + digits[2:]
    return digits


def _clean_blocked_numbers(value):
    if not isinstance(value, list) or len(value) > MAX_BLOCKED_NUMBERS:
        raise ValueError(f'blocked_numbers must be a list of at most {MAX_BLOCKED_NUMBERS} numbers')
    numbers = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError('blocked_numbers must contain text only')
        number = normalize_number(entry)
        if len(number) > MAX_NUMBER_DIGITS:
            raise ValueError(f'not a phone number: {entry[:40]}')
        if number and number not in numbers:
            numbers.append(number)
    return numbers or None


def _clean_work_hours(value):
    try:
        days, start, end = value['days'], value['from'], value['to']
        valid = (
            isinstance(days, list) and days
            and all(type(day) is int and 1 <= day <= 7 for day in days)
            and CLOCK.fullmatch(start) and CLOCK.fullmatch(end)
            and start < end  # ponytail: no overnight shifts; split them into two schedules if ever needed
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise ValueError('work_hours needs days (1-7, Monday = 1) and from < to as HH:MM')
    return {'days': sorted(set(days)), 'from': start, 'to': end}


def clean_setting(key, value):
    """A setting as it will be stored; None means "back to the default". Raises ValueError on anything unexpected."""
    known = key in TEXT_LIMITS or key in CHOICES or key in ('blocked_numbers', 'work_hours', 'voice_id')
    if not known:
        raise ValueError(f'unknown setting: {key}')
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if key in TEXT_LIMITS:
        if not isinstance(value, str) or len(value) > TEXT_LIMITS[key]:
            raise ValueError(f'{key} must be text of at most {TEXT_LIMITS[key]} characters')
        return value.strip()
    if key in CHOICES:
        if value not in CHOICES[key]:
            raise ValueError(f'{key} must be one of: {", ".join(CHOICES[key])}')
        return value
    if key == 'voice_id':
        if not isinstance(value, str) or not VOICE_ID.fullmatch(value):
            raise ValueError('voice_id is not an ElevenLabs voice id')
        return value
    if key == 'blocked_numbers':
        return _clean_blocked_numbers(value)
    return _clean_work_hours(value)
