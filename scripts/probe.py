"""Fake browser/caller for end-to-end checks against a running server and the real APIs.

Usage (from the repo root): python scripts/probe.py PORT STAY_SECONDS [options]
  --say TEXT    spoken (synthesized via ElevenLabs, cached) once the opening turn has finished
  --barge TEXT  spoken --barge-delay seconds after the agent starts replying to --say
  --meta JSON   sent as the first text frame (e.g. '{"type":"callMeta","caller":"0901234567"}')
  --no-login    skip logging in, to check that the server refuses the call
Reads DASHBOARD_PASSWORD and the ElevenLabs settings from the environment or ./.env.
Streams silence the rest of the time, like an open mic. Exits when the server closes
the socket, or after STAY_SECONDS.
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv(Path.cwd() / '.env')

parser = argparse.ArgumentParser()
parser.add_argument('port', type=int)
parser.add_argument('stay', type=float)
parser.add_argument('--say')
parser.add_argument('--barge')
parser.add_argument('--meta')
parser.add_argument('--barge-delay', type=float, default=1.5)
parser.add_argument('--no-login', action='store_true')
args = parser.parse_args()

FRAME = 960  # 480 samples PCM16 = 20ms @ 24kHz
SILENCE = bytes(FRAME)
CACHE = Path(__file__).resolve().parent.parent / 'data' / 'probe-cache'  # data/ is git-ignored


async def synthesize(session, text):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / (hashlib.sha1(text.encode()).hexdigest()[:12] + '.pcm')
    if not path.exists():
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{os.environ['ELEVENLABS_VOICE_ID']}?output_format=pcm_24000"
        async with session.post(
            url,
            headers={'xi-api-key': os.environ['ELEVENLABS_API_KEY']},
            json={'text': text, 'model_id': os.environ.get('ELEVENLABS_MODEL_ID') or 'eleven_flash_v2_5'},
        ) as resp:
            body = await resp.read()
            if resp.status != 200:
                sys.exit(f'PROBE: synth failed {resp.status}: {body[:300]!r}')
            path.write_bytes(body)
    return path.read_bytes()


async def main():
    async with aiohttp.ClientSession() as session:
        say_pcm = await synthesize(session, args.say) if args.say else None
        barge_pcm = await synthesize(session, args.barge) if args.barge else None

        base = f'http://localhost:{args.port}'
        ws = None
        for _ in range(50):  # wait for the server to come up
            try:
                if not args.no_login:
                    await session.post(
                        f'{base}/login', data={'password': os.environ['DASHBOARD_PASSWORD']}, allow_redirects=False,
                    )
                ws = await session.ws_connect(f'{base}/ws')
                break
            except aiohttp.WSServerHandshakeError as refused:
                sys.exit(f'PROBE: server refused the call: HTTP {refused.status}')
            except aiohttp.ClientConnectorError:
                await asyncio.sleep(0.2)
        if ws is None:
            sys.exit('PROBE: could not connect')
        t0 = time.monotonic()

        def ms():
            return int((time.monotonic() - t0) * 1000)

        if args.meta:
            await ws.send_str(args.meta)

        speech = asyncio.Queue()  # PCM buffers waiting to be "spoken" into the mic

        async def mic():
            current = b''
            next_at = time.monotonic()
            while not ws.closed:
                if not current and not speech.empty():
                    current = speech.get_nowait()
                    print(f'PROBE {ms()}ms >>> caller starts speaking ({len(current) / 48000:.1f}s)')
                frame, current = (current[:FRAME], current[FRAME:]) if current else (SILENCE, b'')
                try:
                    await ws.send_bytes(frame.ljust(FRAME, b'\0'))
                except ConnectionError:
                    return
                next_at += 0.02
                await asyncio.sleep(max(0, next_at - time.monotonic()))

        mic_task = asyncio.create_task(mic())
        audio_bytes = 0
        audio_since_state = 0
        listening_count = 0
        speaking_count = 0
        try:
            async with asyncio.timeout(args.stay):
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        if audio_since_state == 0:
                            print(f'PROBE {ms()}ms first audio chunk of this turn')
                        audio_bytes += len(msg.data)
                        audio_since_state += len(msg.data)
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        event = json.loads(msg.data)
                        print(f'PROBE {ms()}ms {json.dumps(event, ensure_ascii=False)}')
                        if event.get('type') == 'state':
                            audio_since_state = 0
                            if event['state'] == 'listening':
                                listening_count += 1
                                if listening_count == 1 and say_pcm:
                                    speech.put_nowait(say_pcm)
                            if event['state'] == 'speaking':
                                speaking_count += 1
                                if speaking_count == 2 and barge_pcm:
                                    asyncio.get_running_loop().call_later(args.barge_delay, speech.put_nowait, barge_pcm)
                    else:
                        break
                print(f'PROBE {ms()}ms server closed the socket (code {ws.close_code})')
        except TimeoutError:
            print(f'PROBE stayed {args.stay}s, hanging up from the caller side')
        finally:
            mic_task.cancel()
            await ws.close()
        print(f'PROBE total audio_bytes={audio_bytes} (~{audio_bytes / 16000:.1f}s of mp3)')


asyncio.run(main())
