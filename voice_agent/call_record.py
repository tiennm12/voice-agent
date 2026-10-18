import json
import logging
import re
import shutil
import wave
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .llm_client import CHAT_MODEL, CHAT_REASONING_EFFORT

log = logging.getLogger(__name__)

SUMMARY_PROMPT = (
    'Bạn nhận transcript một cuộc gọi nhỡ do trợ lý AI nghe máy thay chủ thuê bao. '
    'Trả về một JSON object với đúng 4 khóa: '
    '"caller_name" (tên người gọi, hoặc "Không rõ" nếu họ không nói), '
    '"summary" (1-2 câu tiếng Việt tóm tắt lời nhắn), '
    '"urgency" (một trong: "thấp", "trung bình", "cao"), '
    '"category" ("spam" nếu là telesales, quảng cáo, lừa đảo hoặc cuộc gọi tự động; còn lại là "bình thường"). '
    'Transcript do máy nhận dạng giọng nói tạo ra nên có thể chứa nhiễu: các câu rời rạc, vô nghĩa hoặc không liên quan '
    'nhau là tiếng ồn, không phải lời người gọi. Khi đó summary nói rõ là không nghe rõ lời nhắn, urgency là "thấp", '
    'và không suy diễn. Chỉ chọn urgency "cao" khi người gọi nói rõ ràng việc khẩn cấp.'
)
NORMAL, SPAM = 'bình thường', 'spam'
CALL_FOLDER = re.compile(r'\d{8}T\d{6}_')  # how a call's id, and so its folder's name, starts
NO_MESSAGE = {
    'caller_name': 'Không rõ', 'summary': 'Người gọi không để lại lời nhắn.', 'urgency': 'thấp', 'category': NORMAL,
}
# A call that broke before the caller said anything: "no message" would hide that nobody could hear them.
SYSTEM_FAILURE = {
    'caller_name': 'Không rõ',
    'summary': 'Hệ thống gặp lỗi nên chưa nghe được người gọi. Hãy gọi lại số này.',
    'urgency': 'trung bình',
    'category': NORMAL,
}
# Structured output: the model can only answer with these four keys and these labels.
SUMMARY_FORMAT = {
    'type': 'json_schema',
    'json_schema': {
        'name': 'call_summary',
        'strict': True,
        'schema': {
            'type': 'object',
            'additionalProperties': False,
            'required': ['caller_name', 'summary', 'urgency', 'category'],
            'properties': {
                'caller_name': {'type': 'string'},
                'summary': {'type': 'string'},
                'urgency': {'type': 'string', 'enum': ['thấp', 'trung bình', 'cao']},
                'category': {'type': 'string', 'enum': [NORMAL, SPAM]},
            },
        },
    },
}


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
        # Speech-to-speech calls: already priced when each response finished (see add_realtime_usage).
        'realtime_usd': usage.get('realtime_usd', 0),
    }
    total = sum(costs.values())
    cost = {name: round(value, 6) for name, value in costs.items()} | {'total_usd': round(total, 6)}
    if pricing.get('usd_to_vnd'):
        cost['total_vnd'] = round(total * pricing['usd_to_vnd'])
    return cost


def count_llm_usage(usage_counter, usage):
    # ponytail: cached prompt tokens are billed at full input price here (slight
    # overestimate). Split out prompt_tokens_details.cached_tokens if prompts grow past 1024 tokens.
    usage_counter['llm_input_tokens'] += usage.prompt_tokens
    usage_counter['llm_output_tokens'] += usage.completion_tokens


async def summarize_transcript(openai_client, transcript, session_id='', failed=False):
    """(summary, usage) for a transcript as CallRecord keeps it; usage is None when nothing was billed.

    `failed`: the call hit a system error. Never raises: a failed summary falls back to the caller's own words.
    """
    caller_lines = [turn['text'] for turn in transcript if turn['role'] == 'user' and turn['text'].strip()]
    if not caller_lines:
        return (SYSTEM_FAILURE if failed else NO_MESSAGE), None
    dialogue = '\n'.join(f"{'Người gọi' if turn['role'] == 'user' else 'Trợ lý'}: {turn['text']}" for turn in transcript)
    usage = None
    try:
        response = await openai_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{'role': 'system', 'content': SUMMARY_PROMPT}, {'role': 'user', 'content': dialogue}],
            response_format=SUMMARY_FORMAT,
            **({'reasoning_effort': CHAT_REASONING_EFFORT} if CHAT_REASONING_EFFORT else {}),
        )
        usage = response.usage
        data = json.loads(response.choices[0].message.content)
        summary = {key: str(data[key]) for key in ('caller_name', 'summary', 'urgency')}
        # Spam silences the notification, so only the exact label counts; anything else is a normal call.
        summary['category'] = SPAM if data.get('category') == SPAM else NORMAL
        return summary, usage
    except Exception:
        log.exception("[%s] summary failed, falling back to the caller's own words", session_id)
        return {
            'caller_name': 'Không rõ', 'summary': ' '.join(caller_lines), 'urgency': 'không rõ', 'category': NORMAL,
        }, usage


async def summarize_saved_call(db, openai_client, pricing, call):
    """Finishes a call saved without its summary (the server stopped first): summary, cost, saved. Returns the row."""
    summary, usage = await summarize_transcript(openai_client, call['transcript'] or [], call['session_id'])
    usage_counter = Counter(call['usage'] or {})
    if usage:
        count_llm_usage(usage_counter, usage)
    db.save_call({**call, **summary, 'usage': dict(usage_counter), 'cost': compute_cost(usage_counter, pricing)})
    return db.get_call(call['id'])


def purge_old_calls(db, data_dir, retention_days, tz):
    """Deletes calls older than retention_days, recordings included. Returns the purged ids."""
    cutoff = datetime.now(tz) - timedelta(days=retention_days)
    purged = db.delete_older_than(cutoff.isoformat())
    for call_id in purged:
        if Path(call_id).name == call_id and call_id not in ('.', '..'):  # an id is one path segment, never a path
            shutil.rmtree(Path(data_dir) / 'calls' / call_id, ignore_errors=True)
    # Also by folder name, which starts with the call's start time: a process killed mid-call (out of
    # memory, SIGKILL) leaves a recording with no row, and it must not outlive the retention period either.
    oldest_kept = f'{cutoff:%Y%m%dT%H%M%S}'
    for folder in (Path(data_dir) / 'calls').glob('*'):
        if folder.is_dir() and CALL_FOLDER.match(folder.name) and folder.name[:15] < oldest_kept:
            shutil.rmtree(folder, ignore_errors=True)
    return purged


def back_up_database(db, data_dir, retention_days, tz, keep=7):
    """Today's copy of the database in <data_dir>/backups, keeping the last `keep` days' copies.

    Never more days than the retention period: a backup must not keep a purged call alive much longer.
    ponytail: same disk as the database, so it survives a bad deploy or a corrupt file, not a lost disk;
    recordings are not copied. Sync data_dir/backups elsewhere if that ever matters.
    """
    keep = min(keep, int(retention_days))
    folder = Path(data_dir) / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    if keep:
        db.backup(folder / f'voicemail-{datetime.now(tz):%Y%m%d}.db')
    for old in sorted(folder.glob('voicemail-*.db'))[:-keep or None]:
        old.unlink()


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
        self.failed = False  # a system error reached the caller's side of the call

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
        count_llm_usage(self.usage, usage)

    def add_stt_usage(self, usage):
        if usage.get('type') == 'duration':
            self.usage['stt_seconds'] += usage.get('seconds', 0)
            return
        text_tokens = (usage.get('input_token_details') or {}).get('text_tokens', 0)
        self.usage['stt_text_tokens'] += text_tokens
        self.usage['stt_audio_tokens'] += usage.get('input_tokens', 0) - text_tokens
        self.usage['stt_output_tokens'] += usage.get('output_tokens', 0)

    def add_realtime_usage(self, usage, rates):
        """One speech-to-speech response's usage, priced at once with `rates` (the model's row of pricing['realtime'])."""
        inp, out = usage.get('input_token_details') or {}, usage.get('output_token_details') or {}
        cached = inp.get('cached_tokens_details') or {}
        cached_text, cached_audio = cached.get('text_tokens', 0), cached.get('audio_tokens', 0)
        self.usage['realtime_usd'] += (
            (inp.get('text_tokens', 0) - cached_text) * rates['text_input']
            + cached_text * rates['cached_text_input']
            + (inp.get('audio_tokens', 0) - cached_audio) * rates['audio_input']
            + cached_audio * rates['cached_audio_input']
            + out.get('text_tokens', 0) * rates['text_output']
            + out.get('audio_tokens', 0) * rates['audio_output']
        ) / 1e6

    def set_live_usage(self, seconds, per_minute):
        """A GPT-Live session, billed per second of session. Set, not added: it is reported once, as the call ends."""
        self.usage['live_seconds'] = round(seconds, 1)
        if per_minute is not None:
            self.usage['realtime_usd'] = seconds / 60 * per_minute

    def close(self):
        """Ends the record and saves it. Synchronous on purpose: runs even when the call is torn down by cancellation."""
        self.ended_at = datetime.now(self._tz)
        self._wav.close()
        self._save()

    async def summarize(self, openai_client):
        """Fills in summary and cost, and saves. Never raises: a failed summary falls back to the caller's words."""
        self.summary, usage = await summarize_transcript(openai_client, self.transcript, self.session_id, self.failed)
        if usage:
            self.add_llm_usage(usage)
        self.cost = compute_cost(self.usage, self._pricing)
        self._save()
        log.info('[%s] [cost] total $%.4f %s usage %s', self.session_id, self.cost['total_usd'], self.cost, dict(self.usage))

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
