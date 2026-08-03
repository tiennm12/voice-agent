// src/sessionState.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { createSessionState } = require('./sessionState');

test('walks the happy path: idle -> listening -> thinking -> speaking -> listening', () => {
  const state = createSessionState();
  assert.equal(state.current, 'idle');
  state.transition('CALL_STARTED');
  assert.equal(state.current, 'listening');
  state.transition('TRANSCRIPT_FINAL');
  assert.equal(state.current, 'thinking');
  state.transition('TTS_STARTED');
  assert.equal(state.current, 'speaking');
  state.transition('TTS_ENDED');
  assert.equal(state.current, 'listening');
});

test('USER_INTERRUPTED from speaking goes back to listening', () => {
  const state = createSessionState();
  state.transition('CALL_STARTED');
  state.transition('TRANSCRIPT_FINAL');
  state.transition('TTS_STARTED');
  state.transition('USER_INTERRUPTED');
  assert.equal(state.current, 'listening');
});

test('USER_INTERRUPTED from thinking (before TTS started) also goes back to listening', () => {
  const state = createSessionState();
  state.transition('CALL_STARTED');
  state.transition('TRANSCRIPT_FINAL');
  state.transition('USER_INTERRUPTED');
  assert.equal(state.current, 'listening');
});

test('CALL_ENDED works from any state and returns to idle', () => {
  const state = createSessionState();
  state.transition('CALL_STARTED');
  state.transition('TRANSCRIPT_FINAL');
  state.transition('CALL_ENDED');
  assert.equal(state.current, 'idle');
});

test('an invalid transition throws and does not change state', () => {
  const state = createSessionState();
  assert.throws(() => state.transition('TTS_STARTED'), /Invalid transition/);
  assert.equal(state.current, 'idle');
});

test('onTransition is called with the new state and the event', () => {
  const calls = [];
  const state = createSessionState({ onTransition: (s, e) => calls.push([s, e]) });
  state.transition('CALL_STARTED');
  assert.deepEqual(calls, [['listening', 'CALL_STARTED']]);
});
