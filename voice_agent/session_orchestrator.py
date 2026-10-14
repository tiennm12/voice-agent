import asyncio
import logging
import time
from contextlib import aclosing

from .config import HANGUP_MARKER, build_messages
from .session_state import SessionState
from .text_chunker import TextChunker

log = logging.getLogger(__name__)

LLM_ERROR_APOLOGY = 'Xin lỗi, tôi gặp sự cố kỹ thuật, bạn nói lại được không?'

# Seconds between the goodbye finishing on the caller's end and the hangup.
GOODBYE_GRACE = 0.5


async def _say(text):
    yield text


class SessionOrchestrator:
    """One call: STT events in, LLM tokens chunked into TTS, audio and control messages out.

    Each spoken turn is one asyncio.Task. Barge-in, hangup and upstream drops all
    supersede a turn the same way: cancel its task.
    """

    def __init__(
        self,
        *,
        system_prompt,
        stt_client,
        llm_stream,
        tts_client,
        send_control,
        send_audio,
        session_id='',
        # Spoken verbatim as soon as the call connects, before the caller says
        # anything. None disables it.
        greeting=None,
        # Called (once) when the agent decides the call is over: the conversation
        # wrapped up, the caller went silent, or the call hit max_call.
        on_hangup=None,
        silence_timeout=10.0,
        max_call=180.0,
    ):
        self._system_prompt = system_prompt
        self._stt = stt_client
        self._llm_stream = llm_stream
        self._tts = tts_client
        self._send_control = send_control
        self._send_audio = send_audio
        self._tag = f'[{session_id}]'
        self._greeting = greeting
        self._on_hangup = on_hangup
        self._silence_timeout = silence_timeout
        self._max_call = max_call

        self._state = SessionState(on_transition=self._on_transition)
        self._history = []
        self._turn = None  # the task of the turn still allowed to speak, if any
        self._hangup_after_turn = False  # the reply being spoken is the goodbye
        self._hangup_timer = None
        self._max_call_timer = None
        self._speech_stopped_at = None  # when the caller last went quiet, until a turn claims it
        self._marks = None  # timestamps of the turn whose first audio has not arrived yet
        # ponytail: when the caller will have heard everything sent so far, estimated
        # from bytes sent (ignores network and client buffering — GOODBYE_GRACE and
        # silence_timeout absorb it). Upgrade: have the client report playback end.
        self._playback_ends_at = 0.0

        stt_client.on('speech_started', self._on_speech_started)
        stt_client.on('speech_stopped', self._on_speech_stopped)
        stt_client.on('transcript_final', self._on_transcript_final)
        stt_client.on('error', self._on_upstream_error)
        stt_client.on('disconnected', self._on_stt_disconnected)
        tts_client.on('audio_chunk', self._on_audio_chunk)
        tts_client.on('turn_complete', self._on_tts_turn_complete)
        tts_client.on('error', self._on_upstream_error)
        tts_client.on('disconnected', self._on_tts_disconnected)

    async def start(self):
        # A hard cap, so a call that never wraps up can't hang forever or burn credit.
        self._max_call_timer = asyncio.get_running_loop().call_later(self._max_call, self._hang_up)
        self._state.transition('CALL_STARTED')
        await self._stt.connect()
        # Speak first instead of waiting for the caller to say something — reuses the
        # TRANSCRIPT_FINAL transition (listening -> thinking) since that's exactly the
        # state move a real turn needs, even though no transcript actually arrived.
        if self._greeting and self._state.current == 'listening':
            self._state.transition('TRANSCRIPT_FINAL')
            self._start_turn(self._greeting, is_kickoff=True)

    def stop(self):
        # Abandon first so we don't keep paying for tokens nobody will hear.
        self._abandon_turn()
        if self._max_call_timer:
            self._max_call_timer.cancel()
        self._state.transition('CALL_ENDED')
        self._stt.close()
        self._tts.abort()

    async def handle_audio_chunk(self, data):
        await self._stt.send_audio_chunk(data)

    # --- hanging up --------------------------------------------------------

    def _on_transition(self, state, event):
        self._disarm_hangup()
        if state == 'listening':
            # The agent has nothing more to say for now: either that was the goodbye,
            # or it is the caller's move and staying silent counts against them.
            wait = GOODBYE_GRACE if self._hangup_after_turn else self._silence_timeout
            self._arm_hangup(self._playback_remaining() + wait)

    def _arm_hangup(self, delay):
        self._disarm_hangup()
        self._hangup_timer = asyncio.get_running_loop().call_later(delay, self._hang_up)

    def _disarm_hangup(self):
        if self._hangup_timer:
            self._hangup_timer.cancel()
            self._hangup_timer = None

    def _hang_up(self):
        self._disarm_hangup()
        if self._max_call_timer:
            self._max_call_timer.cancel()
        log.info('%s hanging up', self._tag)
        if self._on_hangup:
            self._on_hangup()

    def _playback_remaining(self):
        return max(0.0, self._playback_ends_at - time.monotonic())

    # --- upstream events -------------------------------------------------

    def _on_speech_started(self):
        # Always flush whatever the browser is still playing/queued, regardless of our
        # own state. ElevenLabs can finish STREAMING a turn's audio well before the
        # browser finishes PLAYING it, so by the time VAD detects the caller talking
        # over the agent, state may already be back to 'listening' even though audio
        # is still audibly playing on the phone.
        self._send_control({'type': 'interrupt'})
        self._playback_ends_at = 0.0
        self._hangup_after_turn = False  # talking over the goodbye keeps the call open
        if self._state.current in ('thinking', 'speaking'):
            self._abandon_turn()
            self._tts.abort()
            self._state.transition('USER_INTERRUPTED')
        # The caller is mid-sentence: silence doesn't count until they stop.
        self._disarm_hangup()

    def _on_speech_stopped(self):
        self._speech_stopped_at = time.monotonic()
        if self._state.current == 'listening':
            self._arm_hangup(self._silence_timeout)

    def _on_transcript_final(self, transcript):
        if self._state.current != 'listening':
            return  # duplicate/out-of-order STT event; only valid from 'listening'
        if not transcript or not transcript.strip():
            return  # VAD fired on noise: nothing was said, so there is nothing to answer
        self._state.transition('TRANSCRIPT_FINAL')
        self._send_control({'type': 'transcript', 'role': 'user', 'text': transcript})
        self._start_turn(transcript)

    def _on_upstream_error(self, err):
        self._send_control({'type': 'error', 'message': str(err)})

    def _on_stt_disconnected(self):
        # Without STT the call can never make progress again: declare it over rather
        # than leave a session that looks alive.
        log.error('%s stt client disconnected unexpectedly', self._tag)
        self._abandon_turn()
        self._tts.abort()
        self._send_control({'type': 'error', 'message': 'Mất kết nối dịch vụ nhận diện giọng nói.'})
        if self._state.current != 'idle':
            self._state.transition('CALL_ENDED')

    def _on_audio_chunk(self, data):
        now = time.monotonic()
        self._playback_ends_at = max(self._playback_ends_at, now) + len(data) / self._tts.bytes_per_second
        self._send_audio(data)
        if self._marks and 'text' in self._marks:  # the first audio of the turn: its latency is now known
            marks, self._marks = self._marks, None
            self._log_latency(marks, now)

    def _log_latency(self, marks, audio_at):
        """One line per turn, splitting up the wait the caller sat through.

        stt_ms: caller stopped talking -> transcript. llm_ms: transcript -> first token.
        tts_ms: first text sent -> first audio back. response_ms: caller stopped -> first audio.
        """
        def ms(start, end):
            return round((end - start) * 1000)

        if marks['kickoff']:
            log.info('%s [latency] greeting response_ms=%d', self._tag, ms(marks['transcript'], audio_at))
            return
        latency = {
            'stt_ms': ms(marks['heard'], marks['transcript']),
            'llm_ms': ms(marks['transcript'], marks.get('token', marks['text'])),
            'tts_ms': ms(marks['text'], audio_at),
            'response_ms': ms(marks['heard'], audio_at),
        }
        log.info('%s [latency] %s', self._tag, ' '.join(f'{name}={value}' for name, value in latency.items()))
        self._send_control({'type': 'latency', **latency})  # the test page shows it

    def _on_tts_turn_complete(self):
        if self._state.current == 'speaking':
            self._state.transition('TTS_ENDED')
            self._send_control({'type': 'state', 'state': 'listening'})

    def _on_tts_disconnected(self):
        log.error('%s tts client disconnected unexpectedly', self._tag)
        self._abandon_turn()
        self._send_control({'type': 'error', 'message': 'Mất kết nối dịch vụ giọng nói.'})
        self._back_to_listening()

    # --- turns -------------------------------------------------------------

    def _start_turn(self, user_text, is_kickoff=False):
        now = time.monotonic()
        # Without a speech_stopped event the clock starts at the transcript.
        self._marks = {'heard': self._speech_stopped_at or now, 'transcript': now, 'kickoff': is_kickoff}
        self._speech_stopped_at = None
        self._turn = asyncio.create_task(self._run_turn(user_text, is_kickoff, self._marks))
        self._turn.add_done_callback(self._on_turn_done)

    def _abandon_turn(self):
        self._marks = None
        turn, self._turn = self._turn, None
        if turn and not turn.done():
            turn.cancel()

    def _back_to_listening(self):
        # For a turn that can no longer speak. TTS_ENDED is only valid from 'speaking';
        # USER_INTERRUPTED is the transition that also gets 'thinking' back to 'listening'.
        if self._state.current in ('thinking', 'speaking'):
            self._state.transition('USER_INTERRUPTED')
            self._send_control({'type': 'state', 'state': 'listening'})

    def _give_up_on_current_turn(self, error_message=None):
        """Called from inside the turn itself, so it must not cancel the task it runs on."""
        self._turn = None
        self._tts.abort()
        if error_message:
            self._send_control({'type': 'error', 'message': error_message})
        self._back_to_listening()

    def _on_turn_done(self, turn):
        if turn.cancelled() or turn.exception() is None:
            return
        log.error('%s turn crashed', self._tag, exc_info=turn.exception())
        if self._turn is turn:
            self._give_up_on_current_turn()

    def _on_tts_connect_done(self, connect, turn):
        if connect.cancelled() or connect.exception() is None or self._turn is not turn:
            return
        # TTS is unreachable for this turn: give up now (LLM call included) instead of
        # finding out when the first chunk is ready to be spoken.
        self._abandon_turn()
        self._send_control({'type': 'error', 'message': str(connect.exception())})
        self._back_to_listening()

    async def _run_turn(self, user_text, is_kickoff, marks):
        """Speak one reply. A kickoff turn speaks user_text itself instead of asking the LLM.

        `marks` collects the timestamps of this turn for the latency log.
        """
        self._hangup_after_turn = False
        assistant_text = ''
        spoken = ''  # assistant_text up to the hangup marker
        speaking = False
        pending = []  # chunks the chunker flushed, not yet sent to TTS
        chunker = TextChunker(on_flush=pending.append)

        # Kick off the TTS handshake and the LLM stream in parallel — serializing them
        # just adds a WebSocket handshake to time-to-first-token on every turn.
        connect = asyncio.create_task(self._tts.connect())
        connect.add_done_callback(lambda task, turn=asyncio.current_task(): self._on_tts_connect_done(task, turn))

        async def speak(text):
            nonlocal speaking
            await connect
            if not speaking:
                speaking = True
                self._state.transition('TTS_STARTED')
                self._send_control({'type': 'state', 'state': 'speaking'})
                marks['text'] = time.monotonic()
            await self._tts.send_text(text)

        async def flush():
            while pending:
                await speak(pending.pop(0))

        try:
            source = _say(user_text) if is_kickoff else self._llm_stream(
                build_messages(self._system_prompt, self._history, user_text),
            )
            async with aclosing(source) as tokens:
                async for token in tokens:
                    if not is_kickoff:
                        marks.setdefault('token', time.monotonic())
                    assistant_text += token
                    # Everything from the first '[' on is the hangup marker (possibly split
                    # across tokens) or stray text after it: never speak it.
                    speakable = assistant_text.split('[', 1)[0]
                    chunker.push(speakable[len(spoken):])
                    spoken = speakable
                    await flush()
            chunker.end()

            self._hangup_after_turn = HANGUP_MARKER in assistant_text
            reply = spoken.strip()
            if reply:
                if not is_kickoff:
                    self._history.append({'role': 'user', 'content': user_text})
                self._history.append({'role': 'assistant', 'content': reply})
                self._send_control({'type': 'transcript', 'role': 'assistant', 'text': reply})

            await flush()
            if speaking:
                await self._tts.end_turn()
            else:
                # Nothing speakable (empty reply, or only the hangup marker): no audio
                # will come back to end the turn.
                self._give_up_on_current_turn()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log.error('%s llm error', self._tag, exc_info=err)
            if connect.done() and not connect.cancelled() and connect.exception() is None:
                # Spoken fallback: apologize in Vietnamese and keep the session alive
                # for the next turn instead of surfacing the raw error message.
                await speak(LLM_ERROR_APOLOGY)
                await self._tts.end_turn()
                self._send_control({'type': 'transcript', 'role': 'assistant', 'text': LLM_ERROR_APOLOGY})
            else:
                # TTS isn't available either (still connecting, or never will be), so
                # nothing can speak the apology.
                self._give_up_on_current_turn(str(err))
        finally:
            connect.cancel()  # no-op once connected
