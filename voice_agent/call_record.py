import json
import logging
import re
import shutil
import wave
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

from .llm_client import CHAT_MODEL

log = logging.getLogger(__name__)

SUMMARY_PROMPT = (
    'Bạn nhận transcript một cuộc gọi nhỡ do trợ lý AI nghe máy thay chủ thuê bao. '
    'Trả về một JSON object với đúng 3 khóa: '
    '"caller_name" (tên người gọi, hoặc "Không rõ" nếu họ không nói), '
    '"summary" (1-2 câu tiếng Việt tóm tắt lời nhắn), '
    '"urgency" (một trong: "thấp", "trung bình", "cao").'
)
NO_MESSAGE = {'caller_name': 'Không rõ', 'summary': 'Người gọi không để lại lời nhắn.', 'urgency': 'thấp'}
TELEGRAM_MAX_TEXT = 4096


def compute_cost(usage, pricing):
    """Estimated USD cost of one call from its usage counters and the configured price table."""
    llm, stt, tts = pricing['llm'], pricing['stt'], pricing['tts']
    costs = {
        'llm_usd': (
            usage['llm_input_tokens'] * llm['input_per_1m_tokens']
            + usage['llm_output_tokens'] * llm['output_per_1m_tokens']
        ) / 1e6,
        # Token-billed models report tokens, duration-billed ones (whisper) report seconds.
        'stt_usd': (
            usage['stt_audio_tokens'] * stt['audio_input_per_1m_tokens']
            + usage['stt_text_tokens'] * stt['text_input_per_1m_tokens']
            + usage['stt_output_tokens'] * stt['output_per_1m_tokens']
        ) / 1e6 + usage['stt_seconds'] / 60 * stt['per_minute'],
        'tts_usd': usage['tts_characters'] / 1000 * tts['per_1k_characters'],
    }
    total = sum(costs.values())
    cost = {name: round(value, 6) for name, value in costs.items()} | {'total_usd': round(total, 6)}
    if pricing.get('usd_to_vnd'):
        cost['total_vnd'] = round(total * pricing['usd_to_vnd'])
    return cost


def purge_old_calls(db, data_dir, retention_days, tz):
    """Deletes calls older than retention_days, recordings included. Returns the purged ids."""
    cutoff = datetime.now(tz) - timedelta(days=retention_days)
    purged = db.delete_older_than(cutoff.isoformat())
    for call_id in purged:
        if Path(call_id).name == call_id and call_id not in ('.', '..'):  # an id is one path segment, never a path
            shutil.rmtree(Path(data_dir) / 'calls' / call_id, ignore_errors=True)
    return purged


class CallRecord:
    """Everything kept about one call: caller audio, transcript, usage, summary, cost.

    The recording lives in <data_dir>/calls/<id>/caller.wav; everything else is a row in the database.
    """

    def __init__(self, *, session_id, data_dir, pricing, db, tz):
        self._pricing = pricing
        self._db = db
        self._tz = tz
        self.session_id = session_id
        self.started_at = datetime.now(tz)
        self.ended_at = None
        self.id = f'{self.started_at:%Y%m%dT%H%M%S}_{session_id}'
        self.dir = Path(data_dir) / 'calls' / self.id
        self.dir.mkdir(parents=True)
        self.caller = None
        self.pbx_session_id = None
        self.transcript = []
        self.summary = None
        self.usage = Counter()
        self.cost = None

        # ponytail: synchronous disk writes on the event loop (a few KB per second per
        # call). Move to a thread if many concurrent calls ever share one process.
        self._wav = wave.open(str(self.dir / 'caller.wav'), 'wb')
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(24000)

    def write_audio(self, data):
        self._wav.writeframesraw(data)

    def add_transcript(self, role, text):
        self.transcript.append({'role': role, 'text': text, 'at': datetime.now(self._tz).isoformat()})

    def set_caller(self, raw):
        """Takes the client's callMeta text frame. Untrusted input: keep only what looks like a phone number."""
        try:
            meta = json.loads(raw)
        except ValueError:
            return
        if not isinstance(meta, dict) or meta.get('type') != 'callMeta':
            return
        self.caller = re.sub(r'[^\d+]', '', str(meta.get('caller') or ''))[:20] or None
        self.pbx_session_id = re.sub(r'[^\w.@-]', '', str(meta.get('pbxSessionId') or ''))[:80] or None

    def add_llm_usage(self, usage):
        # ponytail: cached prompt tokens are billed at full input price here (slight
        # overestimate). Split out prompt_tokens_details.cached_tokens if prompts grow past 1024 tokens.
        self.usage['llm_input_tokens'] += usage.prompt_tokens
        self.usage['llm_output_tokens'] += usage.completion_tokens

    def add_stt_usage(self, usage):
        if usage.get('type') == 'duration':
            self.usage['stt_seconds'] += usage.get('seconds', 0)
            return
        text_tokens = (usage.get('input_token_details') or {}).get('text_tokens', 0)
        self.usage['stt_text_tokens'] += text_tokens
        self.usage['stt_audio_tokens'] += usage.get('input_tokens', 0) - text_tokens
        self.usage['stt_output_tokens'] += usage.get('output_tokens', 0)

    def close(self):
        """Ends the record and saves it. Synchronous on purpose: runs even when the call is torn down by cancellation."""
        self.ended_at = datetime.now(self._tz)
        self._wav.close()
        self._save()

    async def summarize_and_notify(self, *, openai_client, http, telegram_bot_token, telegram_chat_id):
        caller_lines = [turn['text'] for turn in self.transcript if turn['role'] == 'user' and turn['text'].strip()]
        if not caller_lines:
            self.summary = NO_MESSAGE
        else:
            try:
                self.summary = await self._summarize(openai_client)
            except Exception:
                log.exception('[%s] summary failed, falling back to the caller\'s own words', self.session_id)
                self.summary = {'caller_name': 'Không rõ', 'summary': ' '.join(caller_lines), 'urgency': 'không rõ'}

        self.cost = compute_cost(self.usage, self._pricing)
        self._save()
        log.info('[%s] [cost] total $%.4f %s usage %s', self.session_id, self.cost['total_usd'], self.cost, dict(self.usage))

        if telegram_bot_token and telegram_chat_id:
            try:
                await self._notify(http, telegram_bot_token, telegram_chat_id)
            except Exception as err:
                # No traceback, and the token scrubbed: the request URL carries it, and
                # client errors like to quote the URL.
                log.error(
                    '[%s] telegram notification failed: %s %s',
                    self.session_id, type(err).__name__, str(err).replace(telegram_bot_token, '***'),
                )

    async def _summarize(self, openai_client):
        dialogue = '\n'.join(
            f"{'Người gọi' if turn['role'] == 'user' else 'Trợ lý'}: {turn['text']}" for turn in self.transcript
        )
        response = await openai_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{'role': 'system', 'content': SUMMARY_PROMPT}, {'role': 'user', 'content': dialogue}],
            response_format={'type': 'json_object'},
        )
        if response.usage:
            self.add_llm_usage(response.usage)
        data = json.loads(response.choices[0].message.content)
        return {key: str(data[key]) for key in ('caller_name', 'summary', 'urgency')}

    def notification_text(self):
        minutes, seconds = divmod(round((self.ended_at - self.started_at).total_seconds()), 60)
        cost = f"${self.cost['total_usd']:.4f}"
        if 'total_vnd' in self.cost:
            cost += f" (~{self.cost['total_vnd']:,}đ)".replace(',', '.')
        return '\n'.join([
            f"Cuộc gọi nhỡ từ {self.caller or 'số không rõ'} lúc {self.started_at:%H:%M %d/%m/%Y}",
            f"Người gọi: {self.summary['caller_name']}",
            f"Nội dung: {self.summary['summary']}",
            f"Độ khẩn: {self.summary['urgency']}",
            f"Thời lượng: {minutes}:{seconds:02d} · Chi phí: {cost}",
        ])[:TELEGRAM_MAX_TEXT]

    async def _notify(self, http, token, chat_id):
        async with http.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': self.notification_text()},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status != 200:
                # Never log the URL: it carries the bot token.
                log.error('[%s] telegram sendMessage failed: %s %s', self.session_id, response.status, (await response.text())[:300])

    def _save(self):
        summary = self.summary or {}
        self._db.save_call({
            'id': self.id,
            'session_id': self.session_id,
            'caller': self.caller,
            'pbx_session_id': self.pbx_session_id,
            'started_at': self.started_at.isoformat(),
            'ended_at': self.ended_at.isoformat(),
            'duration_seconds': round((self.ended_at - self.started_at).total_seconds()),
            'caller_name': summary.get('caller_name'),
            'summary': summary.get('summary'),
            'urgency': summary.get('urgency'),
            'category': summary.get('category'),
            'transcript': self.transcript,
            'usage': dict(self.usage),
            'cost': self.cost,
        })
