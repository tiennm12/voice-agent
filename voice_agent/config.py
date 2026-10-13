import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The LLM ends its final reply with this marker; the orchestrator strips it from the
# spoken text and hangs up once that reply has been played.
HANGUP_MARKER = '[HANGUP]'


@dataclass(frozen=True)
class Config:
    openai_api_key: str
    openai_realtime_model: str
    openai_transcribe_model: str
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    elevenlabs_model_id: str
    port: int
    owner_name: str
    greeting: str
    system_prompt: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    data_dir: Path
    pricing: dict
    silence_timeout: float
    max_call: float


def build_system_prompt(document_text, owner_name='chủ máy'):
    return '\n'.join([
        'Bạn là trợ lý gọi điện thoại, trả lời bằng tiếng Việt, ngắn gọn, tự nhiên như đang nói chuyện.',
        'Dựa vào tài liệu tham khảo dưới đây để trả lời; nếu không chắc chắn, nói rõ là bạn không có thông tin đó, không bịa.',
        '---',
        document_text.strip().replace('{{OWNER_NAME}}', owner_name),
        '---',
        # Kept here rather than in the editable document: hanging up depends on it.
        f'Khi cuộc trò chuyện đã xong (bạn đã ghi nhận lời nhắn và chào tạm biệt, hoặc người gọi chào tạm biệt), '
        f'hãy kết thúc câu trả lời cuối cùng đó bằng đúng chuỗi {HANGUP_MARKER} để hệ thống cúp máy. '
        f'Không dùng {HANGUP_MARKER} hay dấu ngoặc vuông ở bất kỳ lúc nào khác.',
    ])


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
    for key in ('OPENAI_API_KEY', 'ELEVENLABS_API_KEY', 'ELEVENLABS_VOICE_ID'):
        if not env.get(key):
            raise ValueError(f'Missing required env var: {key}')

    owner_name = env.get('OWNER_NAME') or 'chủ máy'
    return Config(
        openai_api_key=env['OPENAI_API_KEY'],
        openai_realtime_model=env.get('OPENAI_REALTIME_MODEL') or 'gpt-realtime-2.1',
        openai_transcribe_model=env.get('OPENAI_TRANSCRIBE_MODEL') or 'gpt-4o-transcribe',
        elevenlabs_api_key=env['ELEVENLABS_API_KEY'],
        elevenlabs_voice_id=env['ELEVENLABS_VOICE_ID'],
        elevenlabs_model_id=env.get('ELEVENLABS_MODEL_ID') or 'eleven_flash_v2_5',
        port=int(env.get('PORT') or 3000),
        owner_name=owner_name,
        greeting=f'Chào bạn, {owner_name} hiện không thể nghe máy. Mình là trợ lý AI, bạn cần nhắn gì không ạ?',
        system_prompt=build_system_prompt(read_system_doc(), owner_name),
        # Both optional: without them the call is still recorded, just not pushed anywhere.
        telegram_bot_token=env.get('TELEGRAM_BOT_TOKEN') or None,
        telegram_chat_id=env.get('TELEGRAM_CHAT_ID') or None,
        data_dir=Path(env.get('DATA_DIR') or ROOT / 'data'),
        pricing=read_pricing(),
        # Seconds. Phone lines add delay a browser mic doesn't have: tune per deployment.
        silence_timeout=float(env.get('SILENCE_TIMEOUT_SECONDS') or 10),
        max_call=float(env.get('MAX_CALL_SECONDS') or 180),
    )
