const TRANSITIONS = {
  idle: { CALL_STARTED: 'listening' },
  listening: { TRANSCRIPT_FINAL: 'thinking' },
  thinking: { TTS_STARTED: 'speaking', USER_INTERRUPTED: 'listening' },
  speaking: { TTS_ENDED: 'listening', USER_INTERRUPTED: 'listening' },
};

function createSessionState({ onTransition = () => {} } = {}) {
  let current = 'idle';

  function transition(event) {
    if (event === 'CALL_ENDED') {
      current = 'idle';
      onTransition(current, event);
      return current;
    }

    const nextState = TRANSITIONS[current] && TRANSITIONS[current][event];
    if (!nextState) {
      throw new Error(`Invalid transition: event "${event}" not allowed in state "${current}"`);
    }
    current = nextState;
    onTransition(current, event);
    return current;
  }

  return {
    get current() {
      return current;
    },
    transition,
  };
}

module.exports = { createSessionState };
