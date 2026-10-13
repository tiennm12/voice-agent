import pytest

from voice_agent.session_state import SessionState


def test_walks_the_happy_path_idle_listening_thinking_speaking_listening():
    state = SessionState()
    assert state.current == 'idle'
    state.transition('CALL_STARTED')
    assert state.current == 'listening'
    state.transition('TRANSCRIPT_FINAL')
    assert state.current == 'thinking'
    state.transition('TTS_STARTED')
    assert state.current == 'speaking'
    state.transition('TTS_ENDED')
    assert state.current == 'listening'


def test_user_interrupted_from_speaking_goes_back_to_listening():
    state = SessionState()
    state.transition('CALL_STARTED')
    state.transition('TRANSCRIPT_FINAL')
    state.transition('TTS_STARTED')
    state.transition('USER_INTERRUPTED')
    assert state.current == 'listening'


def test_user_interrupted_from_thinking_before_tts_started_also_goes_back_to_listening():
    state = SessionState()
    state.transition('CALL_STARTED')
    state.transition('TRANSCRIPT_FINAL')
    state.transition('USER_INTERRUPTED')
    assert state.current == 'listening'


def test_call_ended_works_from_any_state_and_returns_to_idle():
    state = SessionState()
    state.transition('CALL_STARTED')
    state.transition('TRANSCRIPT_FINAL')
    state.transition('CALL_ENDED')
    assert state.current == 'idle'


def test_an_invalid_transition_throws_and_does_not_change_state():
    state = SessionState()
    with pytest.raises(ValueError, match='Invalid transition'):
        state.transition('TTS_STARTED')
    assert state.current == 'idle'


def test_on_transition_is_called_with_the_new_state_and_the_event():
    calls = []
    state = SessionState(on_transition=lambda s, e: calls.append((s, e)))
    state.transition('CALL_STARTED')
    assert calls == [('listening', 'CALL_STARTED')]
