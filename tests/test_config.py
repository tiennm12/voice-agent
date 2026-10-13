from datetime import timedelta

import pytest

from voice_agent.config import HANGUP_MARKER, build_messages, build_system_prompt, load_config

from fakes import REQUIRED_ENV


def test_build_system_prompt_embeds_the_trimmed_document_text():
    prompt = build_system_prompt('  Giờ mở cửa: 8h-17h.  ')
    assert 'Giờ mở cửa: 8h-17h.' in prompt
    assert '  Giờ mở cửa' not in prompt
    assert 'tiếng Việt' in prompt


def test_load_config_throws_when_a_required_env_var_is_missing():
    with pytest.raises(ValueError, match='OPENAI_API_KEY'):
        load_config(env={}, read_system_doc=lambda: 'doc')


def test_load_config_applies_defaults_for_optional_env_vars():
    config = load_config(env=REQUIRED_ENV, read_system_doc=lambda: 'doc text')
    assert config.openai_realtime_model == 'gpt-realtime-2.1'
    assert config.openai_transcribe_model == 'gpt-4o-transcribe'
    assert config.elevenlabs_model_id == 'eleven_flash_v2_5'
    assert config.port == 3000
    assert 'doc text' in config.system_prompt


def test_load_config_uses_the_injected_read_system_doc_function():
    called = []
    load_config(env=REQUIRED_ENV, read_system_doc=lambda: called.append(True) or 'doc')
    assert called == [True]


def test_build_messages_puts_the_system_prompt_first_then_history_then_the_new_user_turn():
    messages = build_messages(
        'SYS',
        [{'role': 'user', 'content': 'Chào'}, {'role': 'assistant', 'content': 'Chào bạn'}],
        'Giờ mở cửa lúc mấy giờ?',
    )
    assert messages == [
        {'role': 'system', 'content': 'SYS'},
        {'role': 'user', 'content': 'Chào'},
        {'role': 'assistant', 'content': 'Chào bạn'},
        {'role': 'user', 'content': 'Giờ mở cửa lúc mấy giờ?'},
    ]


def test_build_messages_works_with_empty_history():
    assert build_messages('SYS', [], 'Hi') == [
        {'role': 'system', 'content': 'SYS'},
        {'role': 'user', 'content': 'Hi'},
    ]


def test_build_system_prompt_fills_in_the_owner_name_and_appends_the_hangup_rule():
    prompt = build_system_prompt('Trợ lý của {{OWNER_NAME}}, nhận lời nhắn cho {{OWNER_NAME}}.', owner_name='anh Tuấn')
    assert 'Trợ lý của anh Tuấn, nhận lời nhắn cho anh Tuấn.' in prompt
    assert HANGUP_MARKER in prompt


def test_load_config_builds_the_voicemail_greeting_from_the_owner_name():
    config = load_config(env={**REQUIRED_ENV, 'OWNER_NAME': 'anh Tuấn'}, read_system_doc=lambda: 'Của {{OWNER_NAME}}')
    assert config.owner_name == 'anh Tuấn'
    assert config.greeting == 'Chào bạn, anh Tuấn hiện không thể nghe máy. Mình là trợ lý AI, bạn cần nhắn gì không ạ?'
    assert 'Của anh Tuấn' in config.system_prompt


def test_load_config_voicemail_settings_default_to_a_generic_owner_and_no_telegram(tmp_path):
    config = load_config(env=REQUIRED_ENV, read_system_doc=lambda: 'doc')
    assert config.owner_name == 'chủ máy'
    assert config.telegram_bot_token is None
    assert config.telegram_chat_id is None
    assert config.data_dir.name == 'data'

    config = load_config(
        env={**REQUIRED_ENV, 'TELEGRAM_BOT_TOKEN': 't', 'TELEGRAM_CHAT_ID': '42', 'DATA_DIR': str(tmp_path)},
        read_system_doc=lambda: 'doc',
    )
    assert (config.telegram_bot_token, config.telegram_chat_id, config.data_dir) == ('t', '42', tmp_path)


def test_load_config_hangup_timing_is_tunable_per_deployment():
    config = load_config(env=REQUIRED_ENV, read_system_doc=lambda: 'doc')
    assert (config.silence_timeout, config.max_call) == (10.0, 180.0)

    config = load_config(
        env={**REQUIRED_ENV, 'SILENCE_TIMEOUT_SECONDS': '6.5', 'MAX_CALL_SECONDS': '90'}, read_system_doc=lambda: 'doc',
    )
    assert (config.silence_timeout, config.max_call) == (6.5, 90.0)


def test_the_shipped_system_doc_and_pricing_table_load():
    config = load_config(env=REQUIRED_ENV)
    assert '{{OWNER_NAME}}' not in config.system_prompt
    assert 'chủ máy' in config.system_prompt
    for rate in (
        config.pricing['llm']['input_per_1m_tokens'],
        config.pricing['llm']['output_per_1m_tokens'],
        config.pricing['stt']['audio_input_per_1m_tokens'],
        config.pricing['tts']['per_1k_characters'],
    ):
        assert rate > 0


def test_load_config_requires_a_dashboard_password():
    env = {key: value for key, value in REQUIRED_ENV.items() if key != 'DASHBOARD_PASSWORD'}
    with pytest.raises(ValueError, match='DASHBOARD_PASSWORD'):
        load_config(env=env, read_system_doc=lambda: 'doc')


def test_load_config_retention_and_clock_offset_defaults_and_overrides():
    config = load_config(env=REQUIRED_ENV, read_system_doc=lambda: 'doc')
    assert config.dashboard_password == REQUIRED_ENV['DASHBOARD_PASSWORD']
    assert config.retention_days == 30
    assert config.tz.utcoffset(None) == timedelta(hours=7)

    config = load_config(
        env={**REQUIRED_ENV, 'RETENTION_DAYS': '60', 'UTC_OFFSET_HOURS': '9'}, read_system_doc=lambda: 'doc',
    )
    assert config.retention_days == 60
    assert config.tz.utcoffset(None) == timedelta(hours=9)
