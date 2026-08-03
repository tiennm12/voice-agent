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

function makeFailingConnectTtsClient(errorMessage) {
  const emitter = new EventEmitter();
  const calls = { sendText: [], endTurn: 0, abort: 0, connect: 0 };
  return {
    connect: async () => {
      calls.connect += 1;
      throw new Error(errorMessage);
    },
    sendText: (t) => calls.sendText.push(t),
    endTurn: () => {
      calls.endTurn += 1;
    },
    abort: () => {
      calls.abort += 1;
    },
    on: emitter.on.bind(emitter),
    calls,
  };
}

function makeDeferredConnectTtsClient() {
  const emitter = new EventEmitter();
  const calls = { sendText: [], endTurn: 0, abort: 0, connect: 0 };
  let resolveConnect;
  return {
    connect: () => {
      calls.connect += 1;
      return new Promise((resolve) => {
        resolveConnect = resolve;
      });
    },
    sendText: (t) => calls.sendText.push(t),
    endTurn: () => {
      calls.endTurn += 1;
    },
    abort: () => {
      calls.abort += 1;
    },
    on: emitter.on.bind(emitter),
    calls,
    resolveConnect: () => resolveConnect(),
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

test('TTS connect failure surfaces an error and returns to listening instead of hanging in thinking', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeFailingConnectTtsClient('ws connect failed');
  let llmInvoked = false;
  const llmClient = makeFakeLlmClient(() => {
    llmInvoked = true;
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
  sttClient.emitTranscriptFinal('Chào bạn');
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));

  assert.equal(llmInvoked, false);
  assert.ok(controlMessages.some((m) => m.type === 'error' && m.message === 'ws connect failed'));
  assert.ok(controlMessages.some((m) => m.type === 'state' && m.state === 'listening'));

  // Session should be recoverable: a subsequent transcript starts a new turn without throwing.
  assert.doesNotThrow(() => sttClient.emitTranscriptFinal('thử lại'));
});

test('transcriptFinal while not listening is ignored instead of throwing', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeFakeTtsClient();
  const llmClient = makeFakeLlmClient((messages, { onToken, onDone }) => {
    onToken('Xin chào.'); // punctuation triggers an immediate flush -> speaking
    onDone();
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
  sttClient.emitTranscriptFinal('Chào bạn');
  await new Promise((r) => setImmediate(r));

  assert.ok(controlMessages.some((m) => m.type === 'state' && m.state === 'speaking'));
  const transcriptCountBefore = controlMessages.filter((m) => m.type === 'transcript').length;

  assert.doesNotThrow(() => sttClient.emitTranscriptFinal('duplicate, out-of-order transcript'));

  const transcriptCountAfter = controlMessages.filter((m) => m.type === 'transcript').length;
  assert.equal(transcriptCountAfter, transcriptCountBefore, 'the out-of-order transcript should be dropped, not processed');
});

test('barge-in during TTS connect prevents the superseded turn from starting its LLM call', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeDeferredConnectTtsClient();
  let streamCompletionCalls = 0;
  const llmClient = {
    streamCompletion: () => {
      streamCompletionCalls += 1;
      return { abort: () => {} };
    },
    abortCalls: [],
  };

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
  sttClient.emitTranscriptFinal('Kể chuyện đi'); // state -> thinking; ttsClient.connect() is pending

  sttClient.emitSpeechStarted(); // barge-in while still awaiting TTS connect

  ttsClient.resolveConnect(); // now let the superseded connect() settle
  await new Promise((r) => setImmediate(r));

  assert.equal(streamCompletionCalls, 0, 'LLM should never be started for a turn superseded before TTS connected');
  assert.ok(controlMessages.some((m) => m.type === 'interrupt'));
});
