// src/sessionOrchestrator.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createSessionOrchestrator } = require('./sessionOrchestrator');

function makeFakeSttClient() {
  const emitter = new EventEmitter();
  return {
    connect: async () => {},
    sendAudioChunk: () => {},
    close: () => {},
    on: emitter.on.bind(emitter),
    emitSpeechStarted: () => emitter.emit('speechStarted'),
    emitTranscriptFinal: (t) => emitter.emit('transcriptFinal', t),
  };
}

function makeFakeTtsClient() {
  const emitter = new EventEmitter();
  const calls = { sendText: [], endTurn: 0, abort: 0, connect: 0 };
  return {
    connect: async () => {
      calls.connect += 1;
    },
    sendText: (t) => calls.sendText.push(t),
    endTurn: () => {
      calls.endTurn += 1;
    },
    abort: () => {
      calls.abort += 1;
    },
    on: emitter.on.bind(emitter),
    emitAudioChunk: (buf) => emitter.emit('audioChunk', buf),
    emitTurnComplete: () => emitter.emit('turnComplete'),
    calls,
  };
}

function makeFakeLlmClient(scriptFn) {
  const abortCalls = [];
  return {
    streamCompletion: (messages, callbacks) => {
      scriptFn(messages, callbacks);
      return { abort: () => abortCalls.push(true) };
    },
    abortCalls,
  };
}

test('happy path: transcript -> LLM tokens -> chunked TTS -> audio forwarded -> back to listening', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeFakeTtsClient();
  const llmClient = makeFakeLlmClient((messages, { onToken, onDone }) => {
    assert.equal(messages[0].role, 'system');
    onToken('Xin ');
    onToken('chào');
    onToken('.');
    onDone();
  });

  const controlMessages = [];
  const audioMessages = [];
  const orchestrator = createSessionOrchestrator({
    systemPrompt: 'SYS',
    sttClient,
    llmClient,
    ttsClient,
    sendControl: (msg) => controlMessages.push(msg),
    sendAudio: (buf) => audioMessages.push(buf),
  });

  await orchestrator.start();
  sttClient.emitTranscriptFinal('Chào bạn');
  await new Promise((r) => setImmediate(r));

  assert.deepEqual(ttsClient.calls.sendText, ['Xin chào.']);
  assert.equal(ttsClient.calls.endTurn, 1);

  ttsClient.emitAudioChunk(Buffer.from([1, 2]));
  ttsClient.emitTurnComplete();

  assert.deepEqual(audioMessages, [Buffer.from([1, 2])]);
  assert.ok(controlMessages.some((m) => m.type === 'transcript' && m.text === 'Chào bạn'));
  assert.ok(controlMessages.some((m) => m.type === 'state' && m.state === 'speaking'));
  assert.ok(controlMessages.some((m) => m.type === 'state' && m.state === 'listening'));
});

test('barge-in: speechStarted while speaking aborts the LLM and TTS turn', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeFakeTtsClient();
  let capturedCallbacks;
  const llmClient = makeFakeLlmClient((messages, callbacks) => {
    capturedCallbacks = callbacks;
    callbacks.onToken('Đây là một câu trả lời dài dòng dài dòng.'); // triggers a flush -> speaking
  });

  const controlMessages = [];
  const orchestrator = createSessionOrchestrator({
    systemPrompt: 'SYS',
    sttClient,
    llmClient,
    ttsClient,
    sendControl: (msg) => controlMessages.push(msg),
    sendAudio: () => {},
  });

  await orchestrator.start();
  sttClient.emitTranscriptFinal('Kể chuyện đi');
  await new Promise((r) => setImmediate(r));

  sttClient.emitSpeechStarted();

  assert.equal(llmClient.abortCalls.length, 1);
  assert.equal(ttsClient.calls.abort, 1);
  assert.ok(controlMessages.some((m) => m.type === 'interrupt'));
});
