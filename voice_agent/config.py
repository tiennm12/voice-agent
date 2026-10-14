import json
import os
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

from .llm_client import CHAT_MODEL, CHAT_REASONING_EFFORT

ROOT = Path(__file__).resolve().parent.parent

# The LLM ends its final reply with this marker; the orchestrator strips it from the
# spoken text and hangs up once that reply has been played.
HANGUP_MARKER = '[HANGUP]'


@dataclass(frozen=True)
class Config:
    openai_api_key: str
    openai_realtime_model: str
    openai_transcribe_model: str
    openai_chat_model: str
    openai_reasoning_effort: str | None
    vad_silence_ms: int | None
    vad_threshold: float | None
    stt_min_logprob: float | None
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    elevenlabs_model_id: str
    elevenlabs_auto_mode: bool
    port: int
    owner_name: str
    system_doc: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    data_dir: Path
    pricing: dict
    silence_timeout: float
    max_call: float
    dashboard_password: str
    retention_days: float
    utc_offset_hours: float
    public_base_url: str | None
    urgent_reminder_minutes: float

    @property
    def tz(self):
        return timezone(timedelta(hours=self.utc_offset_hours))


def build_system_prompt(document_text, owner_name='chủ máy', language='vi'):
    """language 'vi' pins replies to Vietnamese; 'auto' follows whatever the caller speaks."""
    vietnamese_only = language == 'vi'
    lines = [
        f"Bạn là trợ lý gọi điện thoại, {'trả lời bằng tiếng Việt, ' if vietnamese_only else 'trả lời '}"
        'ngắn gọn, tự nhiên như đang nói chuyện.',
        'Dựa vào tài liệu tham khảo dưới đây để trả lời; nếu không chắc chắn, nói rõ là bạn không có thông tin đó, không bịa.',
        '---',
        document_text.strip().replace('{{OWNER_NAME}}', owner_name),
        '---',
        # Kept here rather than in the editable document: hanging up depends on it.
        f'Khi cuộc trò chuyện đã xong (bạn đã ghi nhận lời nhắn và chào tạm biệt, hoặc người gọi chào tạm biệt), '
        f'hãy kết thúc câu trả lời cuối cùng đó bằng đúng chuỗi {HANGUP_MARKER} để hệ thống cúp máy. '
        f'Không dùng {HANGUP_MARKER} hay dấu ngoặc vuông ở bất kỳ lúc nào khác.',
    ]
    if not vietnamese_only:
        # Stated last and bluntly: against a Vietnamese document and a Vietnamese greeting,
        # a milder hint in the first line was ignored.
        lines.append(
            'QUAN TRỌNG: luôn trả lời bằng đúng ngôn ngữ người gọi vừa dùng. Người gọi nói tiếng Anh thì trả lời '
            'hoàn toàn bằng tiếng Anh, dù tài liệu này và câu chào viết bằng tiếng Việt.'
        )
    return '\n'.join(lines)


def build_messages(system_prompt, history, new_user_text):
    return [
        {'role': 'system', 'content': system_prompt},
        *history,
        {'role': 'user', 'content': new_user_text},
    ]


def _read_system_doc():
    return (ROOT / 'config' / 'systemDoc.md').read_text(encoding='utf-8')


def _read_pricing():
    return json.loads((ROOT / 'config' / 'pricing.json').read_text(encoding='utf-8'))


def load_config(env=None, read_system_doc=_read_system_doc, read_pricing=_read_pricing):
    env = os.environ if env is None else env
    for key in ('OPENAI_API_KEY', 'ELEVENLABS_API_KEY', 'ELEVENLABS_VOICE_ID', 'DASHBOARD_PASSWORD'):
        if not env.get(key):
            raise ValueError(f'Missing required env var: {key}')

    chat_model = env.get('OPENAI_CHAT_MODEL') or CHAT_MODEL
    return Config(
        openai_api_key=env['OPENAI_API_KEY'],
        openai_realtime_model=env.get('OPENAI_REALTIME_MODEL') or 'gpt-realtime-2.1',
        openai_transcribe_model=env.get('OPENAI_TRANSCRIBE_MODEL') or 'gpt-4o-transcribe',
        # The model that talks to the caller. The llm rates in config/pricing.json must match it.
        openai_chat_model=chat_model,
        # The default effort goes with the default model only: a model picked by hand may
        # not take the parameter at all, so it gets none unless one is set for it.
        openai_reasoning_effort=env.get('OPENAI_REASONING_EFFORT')
        or (CHAT_REASONING_EFFORT if chat_model == CHAT_MODEL else None),
        # Turn-detection tuning; unset leaves the OpenAI defaults (500ms of silence, threshold 0.5).
        vad_silence_ms=int(env['VAD_SILENCE_MS']) if env.get('VAD_SILENCE_MS') else None,
        vad_threshold=float(env['VAD_THRESHOLD']) if env.get('VAD_THRESHOLD') else None,
        stt_min_logprob=float(env['STT_MIN_LOGPROB']) if env.get('STT_MIN_LOGPROB') else None,
        elevenlabs_api_key=env['ELEVENLABS_API_KEY'],
        elevenlabs_voice_id=env['ELEVENLABS_VOICE_ID'],
        elevenlabs_model_id=env.get('ELEVENLABS_MODEL_ID') or 'eleven_flash_v2_5',
        elevenlabs_auto_mode=(env.get('ELEVENLABS_AUTO_MODE') or '').lower() in ('1', 'true', 'yes'),
        port=int(env.get('PORT') or 3000),
        # owner_name and system_doc are only the defaults: the dashboard's settings override them per call.
        owner_name=env.get('OWNER_NAME') or 'chủ máy',
        system_doc=read_system_doc(),
        # Both optional: without them the call is still recorded, just not pushed anywhere.
        telegram_bot_token=env.get('TELEGRAM_BOT_TOKEN') or None,
        telegram_chat_id=env.get('TELEGRAM_CHAT_ID') or None,
        data_dir=Path(env.get('DATA_DIR') or ROOT / 'data'),
        pricing=read_pricing(),
        # Seconds. Phone lines add delay a browser mic doesn't have: tune per deployment.
        silence_timeout=float(env.get('SILENCE_TIMEOUT_SECONDS') or 10),
        max_call=float(env.get('MAX_CALL_SECONDS') or 180),
        dashboard_password=env['DASHBOARD_PASSWORD'],
        # Calls older than this are deleted, recording included. 0 keeps nothing past the next purge.
        retention_days=float(env.get('RETENTION_DAYS') or 30),
        # Every stored time uses this one offset (Vietnam, no DST), whatever zone the server's clock is in:
        # it keeps timestamps sortable as text and puts a call on the owner's calendar day.
        utc_offset_hours=float(env.get('UTC_OFFSET_HOURS') or 7),
        # Where a phone reaches this server, e.g. https://vm.example.com. Optional: it only
        # enables the "listen" button under Telegram notifications.
        public_base_url=(env.get('PUBLIC_BASE_URL') or '').rstrip('/') or None,
        urgent_reminder_minutes=float(env.get('URGENT_REMINDER_MINUTES') or 5),
    )
