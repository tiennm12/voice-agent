TRANSITIONS = {
    'idle': {'CALL_STARTED': 'listening'},
    'listening': {'TRANSCRIPT_FINAL': 'thinking'},
    'thinking': {'TTS_STARTED': 'speaking', 'USER_INTERRUPTED': 'listening'},
    'speaking': {'TTS_ENDED': 'listening', 'USER_INTERRUPTED': 'listening'},
}


class SessionState:
    def __init__(self, on_transition=None):
        self.current = 'idle'
        self._on_transition = on_transition or (lambda state, event: None)

    def transition(self, event):
        # CALL_ENDED is valid from any state: a hangup can land at any moment.
        next_state = 'idle' if event == 'CALL_ENDED' else TRANSITIONS.get(self.current, {}).get(event)
        if not next_state:
            raise ValueError(f'Invalid transition: event "{event}" not allowed in state "{self.current}"')
        self.current = next_state
        self._on_transition(self.current, event)
        return self.current
