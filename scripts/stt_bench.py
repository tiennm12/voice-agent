"""Compare streaming speech-to-text providers on Vietnamese speech, in real time.

Usage (from the repo root): python scripts/stt_bench.py [--runs N] [--providers openai,elevenlabs]
Synthesizes each sentence below with ElevenLabs (cached with probe.py's cache), streams it at
real-time speed followed by silence, and prints per provider: end-of-speech -> final transcript
latency (median) and word error rate against the sentence. Reads OPENAI_API_KEY,
ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID from the environment or ./.env.

ponytail: synthetic clean speech flatters every provider; the ranking is what counts. Re-run on
8 kHz phone recordings (data/calls/*/caller.wav) before trusting the absolute WER.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv(Path.cwd() / '.env')

SENTENCES = [
    'Xin chào, tôi là Nam, tôi gọi để nhắn anh Tiến gọi lại cho tôi về hợp đồng nhé.',
    'Cuộc họp ngày mai dời sang ba giờ chiều, nhờ anh xác nhận giúp tôi.',
    'Số điện thoại của tôi là không chín không một hai ba bốn năm sáu bảy.',
    'Chị ơi, bên em giao hàng chậm, mong chị thông cảm cho em.',
    'Việc này gấp, anh nhớ gọi lại cho tôi trước năm giờ chiều nay.',
]
FRAME = 960  # 20 ms of 24 kHz PCM16
TAIL_SECONDS = 3.0


CACHE = Path(__file__).resolve().parent.parent / 'data' / 'probe-cache'  # shared with probe.py


async def synthesize(session, text):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / (hashlib.sha1(text.encode()).hexdigest()[:12] + '.pcm')
    if not path.exists():
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{os.environ['ELEVENLABS_VOICE_ID']}?output_format=pcm_24000"
        async with session.post(
            url, headers={'xi-api-key': os.environ['ELEVENLABS_API_KEY']},
            json={'text': text, 'model_id': os.environ.get('ELEVENLABS_MODEL_ID') or 'eleven_flash_v2_5'},
        ) as resp:
            body = await resp.read()
            if resp.status != 200:
                sys.exit(f'synth failed {resp.status}: {body[:300]!r}')
            path.write_bytes(body)
    return path.read_bytes()


def normalize(text):
    return re.sub(r'[^\w\s]', '', text.lower()).split()


def wer(reference, hypothesis):
    ref, hyp = normalize(reference), normalize(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1] / len(ref)


async def stream(session, url, headers, setup, audio_message, is_final, audio):
    """Send audio at real-time pace; return (final text, seconds from end of speech to the final)."""
    async with session.ws_connect(url, headers=headers, max_msg_size=0) as ws:
        for message in setup:
            await ws.send_json(message)
        speech_end = None
        done = asyncio.Event()
        result = {}

        async def receive():
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                text = is_final(json.loads(message.data))
                if text and speech_end is not None:
                    result['text'], result['at'] = text, time.monotonic()
                    done.set()
                    return

        receiver = asyncio.create_task(receive())
        frames = [audio[i:i + FRAME] for i in range(0, len(audio), FRAME)]
        frames += [bytes(FRAME)] * int(TAIL_SECONDS / 0.02)
        start = time.monotonic()
        for n, frame in enumerate(frames):
            await asyncio.sleep(max(0, start + n * 0.02 - time.monotonic()))
            if n == len(audio) // FRAME:
                speech_end = time.monotonic()
            await ws.send_json(audio_message(frame))
            if done.is_set():
                break
        try:
            await asyncio.wait_for(done.wait(), 5)
        except asyncio.TimeoutError:
            return '', None
        finally:
            receiver.cancel()
        return result['text'], result['at'] - speech_end


async def openai_run(session, audio):
    model = os.environ.get('OPENAI_REALTIME_MODEL') or 'gpt-realtime-2.1'
    transcribe = os.environ.get('OPENAI_TRANSCRIBE_MODEL') or 'gpt-4o-transcribe'
    setup = [{'type': 'session.update', 'session': {
        'type': 'realtime', 'model': model, 'output_modalities': ['text'],
        'audio': {'input': {
            'format': {'type': 'audio/pcm', 'rate': 24000},
            'turn_detection': {'type': 'server_vad', 'create_response': False, 'interrupt_response': False},
            'transcription': {'model': transcribe, 'language': 'vi'},
            'noise_reduction': {'type': 'near_field'},
        }},
    }}]
    return await stream(
        session, f'wss://api.openai.com/v1/realtime?model={model}',
        {'Authorization': f"Bearer {os.environ['OPENAI_API_KEY']}"}, setup,
        lambda frame: {'type': 'input_audio_buffer.append', 'audio': base64.b64encode(frame).decode()},
        lambda event: event.get('transcript') if event.get('type') == 'conversation.item.input_audio_transcription.completed' else None,
        audio,
    )


async def elevenlabs_run(session, audio):
    url = (
        'wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id=scribe_v2_realtime'
        '&audio_format=pcm_24000&language_code=vi&commit_strategy=vad&vad_silence_threshold_secs=0.4'
    )
    return await stream(
        session, url, {'xi-api-key': os.environ['ELEVENLABS_API_KEY']}, [],
        lambda frame: {
            'message_type': 'input_audio_chunk', 'audio_base_64': base64.b64encode(frame).decode(),
            'commit': False, 'sample_rate': 24000,
        },
        lambda event: event.get('text') if event.get('message_type') == 'committed_transcript' else None,
        audio,
    )


PROVIDERS = {'openai': openai_run, 'elevenlabs': elevenlabs_run}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--providers', default=','.join(PROVIDERS))
    args = parser.parse_args()
    async with aiohttp.ClientSession() as session:
        clips = [(text, await synthesize(session, text)) for text in SENTENCES]
        for name in args.providers.split(','):
            latencies, errors = [], []
            for text, audio in clips:
                for _ in range(args.runs):
                    heard, latency = await PROVIDERS[name](session, audio)
                    print(f'  [{name}] {latency and round(latency * 1000)}ms {heard!r}')
                    if latency is not None:
                        latencies.append(latency)
                    errors.append(wer(text, heard))
            median = f'{statistics.median(latencies) * 1000:.0f}ms' if latencies else 'no transcript'
            print(f'{name}: median end-of-speech -> final {median}, mean WER {statistics.mean(errors):.1%}\n')


asyncio.run(main())
