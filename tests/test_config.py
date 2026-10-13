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
    assert config.system_doc == 'doc text'


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


def test_load_config_keeps_the_owner_name_and_the_raw_system_document_as_defaults():
    config = load_config(env={**REQUIRED_ENV, 'OWNER_NAME': 'anh Tuấn'}, read_system_doc=lambda: 'Của {{OWNER_NAME}}')
    assert config.owner_name == 'anh Tuấn'
    assert config.system_doc == 'Của {{OWNER_NAME}}'
    assert 'Của anh Tuấn' in build_system_prompt(config.system_doc, config.owner_name)


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
    system_prompt = build_system_prompt(config.system_doc, config.owner_name)
    assert '{{OWNER_NAME}}' not in system_prompt
    assert 'chủ máy' in system_prompt
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


def test_the_system_prompt_can_tell_the_llm_to_answer_in_the_callers_language():
    vietnamese_only = build_system_prompt('doc')
    follow_caller = build_system_prompt('doc', language='auto')

    assert 'trả lời bằng tiếng Việt' in vietnamese_only
    assert 'trả lời bằng tiếng Việt' not in follow_caller
    assert HANGUP_MARKER in follow_caller
    # A soft hint lost against a Vietnamese persona and greeting (the LLM kept answering in
    # Vietnamese), so the rule is stated last and names the case that matters.
    last_line = follow_caller.splitlines()[-1]
    assert 'ngôn ngữ người gọi' in last_line and 'tiếng Anh' in last_line
    assert vietnamese_only.splitlines()[-1] != last_line


def test_load_config_latency_knobs_are_off_unless_set():
    config = load_config(env=REQUIRED_ENV, read_system_doc=lambda: 'doc')
    assert (config.openai_chat_model, config.vad_silence_ms, config.vad_threshold) == ('gpt-4.1-mini', None, None)

    config = load_config(
        env={**REQUIRED_ENV, 'OPENAI_CHAT_MODEL': 'gpt-4.1-nano', 'VAD_SILENCE_MS': '300', 'VAD_THRESHOLD': '0.7'},
        read_system_doc=lambda: 'doc',
    )
    assert (config.openai_chat_model, config.vad_silence_ms, config.vad_threshold) == ('gpt-4.1-nano', 300, 0.7)


def test_load_config_tts_auto_mode_is_opt_in():
    assert load_config(env=REQUIRED_ENV, read_system_doc=lambda: 'doc').elevenlabs_auto_mode is False
    for value, expected in (('1', True), ('true', True), ('0', False), ('', False)):
        config = load_config(env={**REQUIRED_ENV, 'ELEVENLABS_AUTO_MODE': value}, read_system_doc=lambda: 'doc')
        assert config.elevenlabs_auto_mode is expected, value
