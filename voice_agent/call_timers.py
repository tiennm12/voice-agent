import asyncio
import logging
import time

log = logging.getLogger(__name__)

# Seconds between the goodbye finishing on the caller's end and the hangup.
GOODBYE_GRACE = 0.5
# Seconds before the hard cap at which `on_wrap_up` has the agent say WRAP_UP: enough to speak it.
WRAP_UP_LEAD = 15
WRAP_UP = 'Xin lỗi, cuộc gọi sắp hết thời lượng. Mình đã ghi lại lời nhắn và sẽ báo lại ngay, chào bạn nhé.'


class CallTimers:
    """When a call hangs up on its own: after silence, after the goodbye has played, or at the hard cap.

    Shared by every pipeline. `on_hangup` is called at most once; `on_wrap_up`, if given, WRAP_UP_LEAD
    seconds before the hard cap, so the caller hears a goodbye instead of the line going dead.
    """

    def __init__(self, on_hangup, max_call, tag='', on_wrap_up=None):
        self._on_hangup = on_hangup
        self._max_call = max_call
        self._tag = tag
        self._on_wrap_up = on_wrap_up
        self._timer = None
        self._max_timer = None
        self._wrap_up_timer = None
        self._hung_up = False
        # ponytail: when the caller will have heard everything sent so far, estimated
        # from bytes sent (ignores network and client buffering — GOODBYE_GRACE and
        # silence_timeout absorb it). Upgrade: have the client report playback end.
        self._playback_ends_at = 0.0

    def start(self):
        # A hard cap, so a call that never wraps up can't hang forever or burn credit.
        loop = asyncio.get_running_loop()
        self._max_timer = loop.call_later(self._max_call, self.hang_up)
        if self._on_wrap_up and self._max_call > WRAP_UP_LEAD:
            self._wrap_up_timer = loop.call_later(self._max_call - WRAP_UP_LEAD, self._wrap_up)

    def _wrap_up(self):
        log.info('%s wrapping up before the maximum call length', self._tag)
        self._on_wrap_up()

    def arm(self, delay):
        """Hang up `delay` seconds after the caller has heard everything sent so far."""
        self.disarm()
        self._timer = asyncio.get_running_loop().call_later(self.playback_remaining() + delay, self.hang_up)

    def disarm(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def stop(self):
        """The call is over some other way: nothing may fire any more."""
        self.disarm()
        for timer in (self._max_timer, self._wrap_up_timer):
            if timer:
                timer.cancel()

    def hang_up(self):
        self.stop()
        if self._hung_up:
            return
        self._hung_up = True
        log.info('%s hanging up', self._tag)
        if self._on_hangup:
            self._on_hangup()

    def note_audio(self, size, bytes_per_second):
        self._playback_ends_at = max(self._playback_ends_at, time.monotonic()) + size / bytes_per_second

    def reset_playback(self):
        """The client was told to drop what it still had queued."""
        self._playback_ends_at = 0.0

    def playback_remaining(self):
        return max(0.0, self._playback_ends_at - time.monotonic())
