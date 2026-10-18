import asyncio
import logging
from types import SimpleNamespace

from fakes import FakeTts, tick
from voice_agent import call_timers, session_orchestrator
from voice_agent.call_timers import WRAP_UP
from voice_agent.session_orchestrator import LLM_ERROR_APOLOGY, SessionOrchestrator

DONE = object()


class FakeStt:
    def __init__(self):
        self._handlers = {}
        self.closes = 0

    async def connect(self):
        pass

    async def send_audio_chunk(self, data):
        pass

    def close(self):
        self.closes += 1

    def on(self, name, handler):
        self._handlers[name] = handler

    def emit(self, name, *args):
        self._handlers[name](*args)


class FakeLlm:
    """Each call streams `script`, then whatever the test pushes with feed()/finish()/fail().

    Script items: a str is a token, DONE ends the stream, an Exception is raised.
    `cancelled` counts streams closed before they finished (the abort signal).
    """

    def __init__(self, *script):
        self._script = script
        self._queue = asyncio.Queue()
        self.calls = []
        self.cancelled = 0

    def feed(self, token):
        self._queue.put_nowait(token)

    def finish(self):
        self._queue.put_nowait(DONE)

    def fail(self, error):
        self._queue.put_nowait(error)

    def __call__(self, messages):
        self.calls.append(messages)
        return self._stream()

    async def _stream(self):
        completed = False
        try:
            scripted = iter(self._script)
            while True:
                item = next(scripted, None)
                if item is None:
                    item = await self._queue.get()
                if item is DONE:
                    completed = True
                    return
                if isinstance(item, Exception):
                    completed = True
                    raise item
                yield item
        finally:
            if not completed:
                self.cancelled += 1


def make(llm=None, tts=None, **kwargs):
    ctx = SimpleNamespace(
        stt=FakeStt(), tts=tts or FakeTts(), llm=llm or FakeLlm(), control=[], audio=[], hangups=[],
    )
    ctx.orchestrator = SessionOrchestrator(
        system_prompt='SYS',
        stt_client=ctx.stt,
        llm_stream=ctx.llm,
        tts_client=ctx.tts,
        send_control=ctx.control.append,
        send_audio=ctx.audio.append,
        on_hangup=lambda: ctx.hangups.append(True),
        **kwargs,
    )
    return ctx


def has(control, **fields):
    return any(all(msg.get(k) == v for k, v in fields.items()) for msg in control)


async def test_happy_path_transcript_llm_tokens_chunked_tts_audio_forwarded_back_to_listening():
    ctx = make(llm=FakeLlm('Xin ', 'chào', '.', DONE))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()

    assert ctx.llm.calls[0][0]['role'] == 'system'
    assert ctx.tts.sent_text == ['Xin chào.']
    assert ctx.tts.end_turns == 1

    ctx.tts.emit('audio_chunk', bytes([1, 2]))
    ctx.tts.emit('turn_complete')

    assert ctx.audio == [bytes([1, 2])]
    assert has(ctx.control, type='transcript', role='user', text='Chào bạn')
    assert has(ctx.control, type='transcript', role='assistant', text='Xin chào.')
    assert has(ctx.control, type='state', state='speaking')
    assert has(ctx.control, type='state', state='listening')


async def test_greeting_is_spoken_verbatim_without_an_llm_call_and_stored_in_history_as_the_assistant():
    ctx = make(llm=FakeLlm('Dạ.', DONE), greeting='Chào bạn, mình là trợ lý AI. Bạn cần nhắn gì không ạ?')

    await ctx.orchestrator.start()
    await tick()

    assert ctx.llm.calls == [], 'a fixed greeting must not cost an LLM round trip'
    assert ctx.tts.sent_text == ['Chào bạn,', 'mình là trợ lý AI.', 'Bạn cần nhắn gì không ạ?']
    assert ctx.tts.end_turns == 1
    assert not has(ctx.control, type='transcript', role='user')
    assert has(ctx.control, type='transcript', role='assistant', text='Chào bạn, mình là trợ lý AI. Bạn cần nhắn gì không ạ?')
    assert has(ctx.control, type='state', state='speaking')

    ctx.tts.emit('turn_complete')
    ctx.stt.emit('transcript_final', 'Tôi muốn nhắn lại')
    await tick()

    assert ctx.llm.calls[0] == [
        {'role': 'system', 'content': 'SYS'},
        {'role': 'assistant', 'content': 'Chào bạn, mình là trợ lý AI. Bạn cần nhắn gì không ạ?'},
        {'role': 'user', 'content': 'Tôi muốn nhắn lại'},
    ]


async def test_greeting_disabled_by_default_does_not_speak_on_start():
    ctx = make()

    await ctx.orchestrator.start()
    await tick()

    assert ctx.llm.calls == []
    assert ctx.tts.connects == 0


async def test_barge_in_speech_started_while_speaking_aborts_the_llm_and_tts_turn():
    ctx = make(llm=FakeLlm('Đây là một câu trả lời dài dòng dài dòng.'))  # flushes -> speaking; stream stays open

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Kể chuyện đi')
    await tick()
    assert has(ctx.control, type='state', state='speaking')

    ctx.stt.emit('speech_started')
    await tick()

    assert ctx.llm.cancelled == 1
    assert ctx.tts.aborts == 1
    assert has(ctx.control, type='interrupt')


async def test_a_barged_in_turn_keeps_the_callers_words_and_what_was_already_said_in_history():
    # VAD splits "Tôi là Nam, ... gọi về hợp đồng" into two turns on a short pause; the
    # second must not reach the LLM without the first.
    ctx = make(llm=FakeLlm('Chào Nam. Bạn cần'))  # first clause spoken, stream stays open

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Tôi là Nam')
    await tick()
    assert ctx.tts.sent_text == ['Chào Nam.']

    ctx.stt.emit('speech_started')
    await tick()
    ctx.stt.emit('transcript_final', 'gọi về hợp đồng')
    await tick()

    assert ctx.llm.calls[1] == [
        {'role': 'system', 'content': 'SYS'},
        {'role': 'user', 'content': 'Tôi là Nam'},
        {'role': 'assistant', 'content': 'Chào Nam.'},
        {'role': 'user', 'content': 'gọi về hợp đồng'},
    ]
    assert has(ctx.control, type='transcript', role='assistant', text='Chào Nam.')


async def test_a_turn_barged_in_before_saying_anything_still_keeps_the_callers_words():
    ctx = make()  # no tokens: the turn is still thinking when the caller talks again

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Tôi là Nam')
    await tick()
    ctx.stt.emit('speech_started')
    await tick()
    ctx.stt.emit('transcript_final', 'gọi lại nhé')
    await tick()

    assert ctx.llm.calls[1][1:] == [
        {'role': 'user', 'content': 'Tôi là Nam'},
        {'role': 'user', 'content': 'gọi lại nhé'},
    ]
    assert not has(ctx.control, type='transcript', role='assistant')


async def test_barge_in_speech_started_after_the_turn_already_completed_still_flushes_client_playback():
    # Regression: ElevenLabs can finish STREAMING a reply well before the browser
    # finishes PLAYING it, so state may already be 'listening' again by the time
    # VAD reports the caller talking over audio that is still audibly playing.
    # The client-side flush must not be gated on backend state.
    ctx = make(llm=FakeLlm('Xin chào.', DONE))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()
    ctx.tts.emit('turn_complete')  # TTS server finished streaming -> state back to 'listening'

    ctx.control.clear()
    ctx.stt.emit('speech_started')
    await tick()

    assert has(ctx.control, type='interrupt')
    # Nothing was actually in flight, so no abort should have happened.
    assert ctx.llm.cancelled == 0
    assert ctx.tts.aborts == 0


async def test_tts_connect_failure_surfaces_an_error_aborts_the_parallel_llm_call_and_returns_to_listening():
    ctx = make(tts=FakeTts(connect_error=RuntimeError('ws connect failed')))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()

    # The LLM call started in parallel with tts.connect(); since TTS can never deliver
    # this turn, the now-pointless LLM call is aborted instead of left running.
    assert len(ctx.llm.calls) == 1
    assert ctx.llm.cancelled == 1
    assert has(ctx.control, type='error', message='ws connect failed')
    assert has(ctx.control, type='state', state='listening')

    # Session is recoverable: a subsequent transcript starts a new turn.
    ctx.stt.emit('transcript_final', 'thử lại')
    await tick()
    assert len(ctx.llm.calls) == 2


async def test_transcript_final_while_not_listening_is_ignored_instead_of_throwing():
    ctx = make(llm=FakeLlm('Xin chào.', DONE))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()
    assert has(ctx.control, type='state', state='speaking')
    transcripts_before = [m for m in ctx.control if m['type'] == 'transcript']

    ctx.stt.emit('transcript_final', 'duplicate, out-of-order transcript')
    await tick()

    assert [m for m in ctx.control if m['type'] == 'transcript'] == transcripts_before
    assert len(ctx.llm.calls) == 1


async def test_an_empty_transcript_from_line_noise_does_not_start_a_turn():
    ctx = make()

    await ctx.orchestrator.start()
    for nothing in ('', '   ', None):
        ctx.stt.emit('transcript_final', nothing)
    await tick()

    assert ctx.llm.calls == []
    assert not has(ctx.control, type='transcript')

    ctx.stt.emit('transcript_final', 'Alo')
    await tick()
    assert len(ctx.llm.calls) == 1, 'still listening: a real utterance starts a turn'


async def test_barge_in_during_tts_connect_aborts_the_already_started_llm_call_and_ignores_the_late_tts_connect_resolution():
    ctx = make(tts=FakeTts(deferred=True))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Kể chuyện đi')  # state -> thinking; tts.connect() is pending
    await tick()
    assert len(ctx.llm.calls) == 1, 'LLM should start without waiting on TTS connect'

    ctx.stt.emit('speech_started')  # barge-in while still awaiting TTS connect
    await tick()
    assert ctx.llm.cancelled == 1, 'the in-flight LLM call for the superseded turn is aborted'

    ctx.tts.resolve_connect()  # now let the superseded connect() settle
    await tick()

    assert len(ctx.llm.calls) == 1, 'no second LLM call is started for the superseded turn'
    assert ctx.tts.sent_text == [], 'nothing is sent to the now-irrelevant TTS connection'
    assert has(ctx.control, type='interrupt')


async def test_llm_stream_starts_in_parallel_with_tts_connect_not_after_it():
    ctx = make(llm=FakeLlm('Xin chào.'), tts=FakeTts(deferred=True))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()

    assert len(ctx.llm.calls) == 1  # called while tts.connect() is still pending
    assert ctx.tts.connects == 1
    assert ctx.tts.sent_text == [], 'the flushed chunk must wait, not be sent before TTS is ready'

    ctx.tts.resolve_connect()
    await tick()

    assert ctx.tts.sent_text == ['Xin chào.'], 'the waiting chunk is sent once TTS connects'


async def test_llm_error_after_tts_is_connected_speaks_a_vietnamese_apology_instead_of_the_raw_error():
    ctx = make()

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()  # tts.connect() has resolved by now

    ctx.llm.fail(RuntimeError('boom'))
    await tick()

    assert ctx.tts.sent_text == [LLM_ERROR_APOLOGY]
    assert ctx.tts.end_turns == 1
    assert not has(ctx.control, type='error', message='boom'), 'the raw error must not be shown to the user'
    assert has(ctx.control, type='state', state='speaking')
    assert has(ctx.control, type='transcript', role='assistant', text=LLM_ERROR_APOLOGY)


async def test_an_llm_that_never_starts_answering_is_covered_by_the_apology(monkeypatch):
    monkeypatch.setattr(session_orchestrator, 'FIRST_TOKEN_TIMEOUT', 0.05)
    ctx = make()  # the stream never yields a token

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await asyncio.sleep(0.15)

    assert ctx.tts.sent_text == [LLM_ERROR_APOLOGY]
    assert ctx.llm.cancelled == 1, 'the hung stream is closed'


async def test_a_long_reply_is_not_cut_off_by_the_first_token_timeout(monkeypatch):
    monkeypatch.setattr(session_orchestrator, 'FIRST_TOKEN_TIMEOUT', 0.05)
    ctx = make(llm=FakeLlm('Dạ, '))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await asyncio.sleep(0.15)
    ctx.llm.feed('mình ghi nhận rồi.')
    ctx.llm.finish()
    await tick()

    assert ctx.tts.sent_text == ['Dạ,', 'mình ghi nhận rồi.']


async def test_llm_error_while_tts_is_still_connecting_falls_back_to_an_error_control_message():
    ctx = make(tts=FakeTts(deferred=True))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')  # TTS connect still pending
    await tick()

    ctx.llm.fail(RuntimeError('boom'))
    await tick()

    assert has(ctx.control, type='error', message='boom')
    assert has(ctx.control, type='state', state='listening')
    assert ctx.tts.aborts == 1

    # The superseded TTS connect resolving later is a no-op, not a crash.
    ctx.tts.resolve_connect()
    await tick()
    assert ctx.tts.sent_text == []


async def test_stop_during_thinking_aborts_the_in_flight_llm_call_and_ignores_a_later_tts_connect_resolution():
    ctx = make(tts=FakeTts(deferred=True))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Kể chuyện đi')  # state -> thinking; TTS connect still pending
    await tick()

    ctx.orchestrator.stop()  # browser hung up mid-"thinking"
    await tick()
    assert ctx.llm.cancelled == 1, 'the in-flight LLM call must be aborted on hangup'

    ctx.tts.resolve_connect()
    await tick()
    assert ctx.tts.sent_text == []


async def test_stop_closes_the_stt_session_and_aborts_tts():
    ctx = make()
    await ctx.orchestrator.start()

    ctx.orchestrator.stop()

    assert ctx.stt.closes == 1
    assert ctx.tts.aborts == 1


async def test_stt_client_disconnected_sends_an_error_and_hangs_up_once():
    ctx = make()
    await ctx.orchestrator.start()

    ctx.stt.emit('disconnected')
    assert has(ctx.control, type='error', message='Mất kết nối dịch vụ nhận diện giọng nói.')
    # Nothing can hear the caller any more: end the call instead of leaving dead air until max_call.
    assert ctx.hangups == [True]

    # The session is now dormant rather than throwing on any further event.
    ctx.stt.emit('disconnected')
    assert ctx.hangups == [True]


async def test_tts_client_disconnected_while_speaking_sends_an_error_and_returns_to_listening():
    ctx = make(llm=FakeLlm('Xin chào.'))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()
    assert has(ctx.control, type='state', state='speaking')

    ctx.tts.emit('disconnected')

    assert has(ctx.control, type='error', message='Mất kết nối dịch vụ giọng nói.')
    assert has(ctx.control, type='state', state='listening')


async def test_stt_client_disconnected_during_an_active_turn_abandons_the_turn_instead_of_leaving_it_to_speak_later():
    ctx = make(llm=FakeLlm('Xin chào.'), tts=FakeTts(deferred=True))  # a chunk waits on the pending TTS connect

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Kể chuyện đi')
    await tick()

    ctx.stt.emit('disconnected')
    await tick()

    assert ctx.llm.cancelled == 1, 'the in-flight LLM call must be aborted'
    assert ctx.tts.aborts == 1, 'the TTS turn must be aborted too, not left dangling'
    assert has(ctx.control, type='error', message='Mất kết nối dịch vụ nhận diện giọng nói.')

    # The deferred TTS connect resolving afterward must be a no-op: not a crash, and
    # not the agent speaking on a call that has already been declared over.
    ctx.tts.resolve_connect()
    await tick()
    assert ctx.tts.sent_text == []


async def test_tts_client_disconnected_while_still_thinking_recovers_to_listening_instead_of_wedging_forever():
    ctx = make()  # no tokens yet -> state stays 'thinking'

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()

    ctx.tts.emit('disconnected')

    assert has(ctx.control, type='error', message='Mất kết nối dịch vụ giọng nói.')
    assert has(ctx.control, type='state', state='listening')

    # The next turn must actually start, proving the session is not wedged.
    ctx.stt.emit('transcript_final', 'thử lại')
    await tick()
    assert has(ctx.control, type='transcript', role='user', text='thử lại')
    assert len(ctx.llm.calls) == 2


async def test_a_turn_that_crashes_returns_to_listening_instead_of_wedging():
    class BrokenTts(FakeTts):
        async def send_text(self, text):
            raise RuntimeError('socket exploded')

    ctx = make(llm=FakeLlm('Xin chào.', DONE), tts=BrokenTts())

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()

    assert has(ctx.control, type='state', state='listening')
    ctx.stt.emit('transcript_final', 'thử lại')
    await tick()
    assert len(ctx.llm.calls) == 2


async def test_an_empty_llm_reply_returns_to_listening_instead_of_wedging_in_thinking():
    ctx = make(llm=FakeLlm(DONE))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()

    assert ctx.tts.sent_text == []
    assert has(ctx.control, type='state', state='listening')

    ctx.stt.emit('transcript_final', 'thử lại')
    await tick()
    assert len(ctx.llm.calls) == 2


# --- auto-hangup ---------------------------------------------------------
# Timers are real but tiny; waits leave a wide margin for Windows' ~16ms timer tick.


async def test_the_hangup_marker_is_never_spoken_and_the_call_ends_once_the_goodbye_has_played():
    ctx = make(llm=FakeLlm('Cảm ơn bạn, ', 'chào bạn nhé. ', '[HANG', 'UP]', DONE))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Thế thôi, chào nhé')
    await tick()

    assert ctx.tts.sent_text == ['Cảm ơn bạn,', 'chào bạn nhé.']
    assert has(ctx.control, type='transcript', role='assistant', text='Cảm ơn bạn, chào bạn nhé.')
    assert ctx.hangups == [], 'must not hang up before the goodbye audio has been delivered'

    ctx.tts.emit('turn_complete')
    await asyncio.sleep(0.8)  # goodbye grace period is 0.5s

    assert ctx.hangups == [True]


async def test_text_the_llm_adds_after_the_hangup_marker_is_dropped():
    ctx = make(llm=FakeLlm('Chào bạn. [HANGUP] (ghi chú nội bộ)', DONE))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào nhé')
    await tick()

    assert ctx.tts.sent_text == ['Chào bạn.']
    assert has(ctx.control, type='transcript', role='assistant', text='Chào bạn.')


async def test_the_caller_speaking_over_the_goodbye_cancels_the_hangup():
    ctx = make(llm=FakeLlm('Chào bạn nhé. [HANGUP]', DONE))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Thế thôi')
    await tick()
    ctx.tts.emit('turn_complete')
    ctx.stt.emit('speech_started')  # "à quên..."
    await asyncio.sleep(0.8)

    assert ctx.hangups == []


async def test_a_reply_that_is_only_the_hangup_marker_still_ends_the_call():
    ctx = make(llm=FakeLlm('[HANGUP]', DONE))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Tạm biệt')
    await asyncio.sleep(0.8)

    assert ctx.tts.sent_text == []
    assert ctx.hangups == [True]


async def test_hangs_up_when_the_caller_stays_silent():
    ctx = make(silence_timeout=0.05)

    await ctx.orchestrator.start()
    await asyncio.sleep(0.25)

    assert ctx.hangups == [True]


async def test_the_silence_timer_starts_when_the_agents_audio_finishes_playing_not_when_it_finishes_streaming():
    ctx = make(llm=FakeLlm('Xin chào.', DONE), silence_timeout=0.05)

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()
    ctx.tts.emit('audio_chunk', bytes(8000))  # 0.5s of 128kbps mp3, delivered in one burst
    ctx.tts.emit('turn_complete')

    await asyncio.sleep(0.3)
    assert ctx.hangups == [], 'the caller is still hearing the reply'
    await asyncio.sleep(0.5)
    assert ctx.hangups == [True]


async def test_the_caller_speaking_pauses_the_silence_timer_until_they_stop():
    ctx = make(silence_timeout=0.05)

    await ctx.orchestrator.start()
    ctx.stt.emit('speech_started')
    await asyncio.sleep(0.25)
    assert ctx.hangups == [], 'a long uninterrupted message must not be cut off'

    ctx.stt.emit('speech_stopped')
    await asyncio.sleep(0.25)
    assert ctx.hangups == [True]


async def test_hangs_up_at_the_maximum_call_duration_even_mid_turn():
    ctx = make(llm=FakeLlm('Đang nói dở'), max_call=0.05)  # stream never finishes

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Kể chuyện đi')
    await asyncio.sleep(0.25)

    assert ctx.hangups == [True]


async def test_shortly_before_the_cap_the_agent_says_goodbye_over_its_reply_and_hangs_up(monkeypatch):
    monkeypatch.setattr(call_timers, 'WRAP_UP_LEAD', 1.0)
    # Wrap-up at 0.2 s, cap at 1.2 s; the reply's stream never finishes.
    ctx = make(llm=FakeLlm('Đang kể một chuyện rất dài'), max_call=1.2)

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Kể chuyện đi')
    await tick()
    await asyncio.sleep(0.25)

    assert ctx.llm.cancelled == 1, 'the reply in progress gives way'
    assert has(ctx.control, type='interrupt')
    assert has(ctx.control, type='transcript', role='assistant', text=WRAP_UP)
    assert ctx.tts.sent_text[-1] == 'chào bạn nhé.'
    ctx.tts.emit('turn_complete')
    await asyncio.sleep(0.6)  # GOODBYE_GRACE
    assert ctx.hangups == [True], 'hung up after the goodbye, before the cap'


async def test_no_wrap_up_on_a_cap_too_short_to_say_it():
    ctx = make(max_call=0.05)

    await ctx.orchestrator.start()
    await asyncio.sleep(0.15)

    assert ctx.hangups == [True]
    assert not has(ctx.control, type='transcript', role='assistant', text=WRAP_UP)


async def test_stop_cancels_the_pending_hangup_timers():
    ctx = make(silence_timeout=0.05, max_call=0.05)

    await ctx.orchestrator.start()
    ctx.orchestrator.stop()
    await asyncio.sleep(0.25)

    assert ctx.hangups == []


async def test_playback_time_is_estimated_from_the_tts_clients_audio_byte_rate():
    tts = FakeTts()
    tts.bytes_per_second = 48000  # pcm_24000
    ctx = make(llm=FakeLlm('Xin chào.', DONE), tts=tts, silence_timeout=0.05)

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()
    ctx.tts.emit('audio_chunk', bytes(24000))  # 0.5s of PCM (it would be 1.5s of mp3)
    ctx.tts.emit('turn_complete')

    await asyncio.sleep(0.3)
    assert ctx.hangups == [], 'the caller is still hearing the reply'
    await asyncio.sleep(0.5)
    assert ctx.hangups == [True], '0.5s of audio + 0.05s of silence have long passed'


# --- latency log -----------------------------------------------------------


def latency_lines(caplog):
    return [record.getMessage() for record in caplog.records if '[latency]' in record.getMessage()]


async def test_each_reply_logs_where_its_response_time_went(caplog):
    caplog.set_level(logging.INFO)
    ctx = make(llm=FakeLlm('Xin chào.', DONE))

    await ctx.orchestrator.start()
    ctx.stt.emit('speech_stopped')  # the caller finished talking…
    await asyncio.sleep(0.06)
    ctx.stt.emit('transcript_final', 'Chào bạn')  # …the transcript arrives 60ms later
    await tick()
    ctx.tts.emit('audio_chunk', b'first')
    ctx.tts.emit('audio_chunk', b'second')

    [line] = latency_lines(caplog)  # once per turn, at the first audio
    fields = dict(part.split('=') for part in line.split()[2:])
    assert set(fields) == {'stt_ms', 'llm_ms', 'tts_ms', 'response_ms'}
    assert int(fields['stt_ms']) >= 50
    assert int(fields['response_ms']) >= int(fields['stt_ms'])


async def test_a_reply_without_a_speech_stopped_event_counts_from_the_transcript(caplog):
    caplog.set_level(logging.INFO)
    ctx = make(llm=FakeLlm('Xin chào.', DONE))

    await ctx.orchestrator.start()
    ctx.stt.emit('transcript_final', 'Chào bạn')
    await tick()
    ctx.tts.emit('audio_chunk', b'first')

    [line] = latency_lines(caplog)
    assert 'stt_ms=0 ' in line


async def test_the_greeting_logs_its_time_to_first_audio(caplog):
    caplog.set_level(logging.INFO)
    ctx = make(greeting='Chào bạn.')

    await ctx.orchestrator.start()
    await tick()
    ctx.tts.emit('audio_chunk', b'first')

    [line] = latency_lines(caplog)
    assert ' greeting ' in line and 'response_ms=' in line
