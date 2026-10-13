from voice_agent.auth import make_token, session_secret, token_is_valid

SECRET = session_secret('correct horse')
NOW = 1_800_000_000


def test_a_fresh_token_is_valid_for_its_subject():
    token = make_token(SECRET, 'session', lifetime=3600, now=NOW)

    assert token_is_valid(SECRET, 'session', token, now=NOW + 3599)


def test_a_token_expires():
    token = make_token(SECRET, 'session', lifetime=3600, now=NOW)

    assert not token_is_valid(SECRET, 'session', token, now=NOW + 3601)


def test_a_token_cannot_have_its_expiry_extended():
    token = make_token(SECRET, 'session', lifetime=3600, now=NOW)
    expires, signature = token.split('.')

    forged = f'{int(expires) + 10_000_000}.{signature}'

    assert not token_is_valid(SECRET, 'session', forged, now=NOW)


def test_a_token_only_works_for_the_subject_it_was_issued_for():
    token = make_token(SECRET, 'call:abc', lifetime=3600, now=NOW)

    assert token_is_valid(SECRET, 'call:abc', token, now=NOW)
    assert not token_is_valid(SECRET, 'call:xyz', token, now=NOW)
    assert not token_is_valid(SECRET, 'session', token, now=NOW)


def test_changing_the_password_invalidates_existing_tokens():
    token = make_token(SECRET, 'session', lifetime=3600, now=NOW)

    assert not token_is_valid(session_secret('new password'), 'session', token, now=NOW)


def test_garbage_is_rejected_without_raising():
    for garbage in ('', 'abc', '.', '123.', 'x.y', None, '9999999999.deadbeef', '1.2.3', '9999999999.chữký'):
        assert not token_is_valid(SECRET, 'session', garbage, now=NOW)
