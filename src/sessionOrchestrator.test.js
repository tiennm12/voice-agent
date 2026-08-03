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
    emitDisconnected: () => emitter.emit('disconnected'),
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
    emitDisconnected: () => emitter.emit('disconnected'),
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

test('opening greeting: openingGreeting speaks first with no preceding transcript, and history stores it under the assistant role only', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeFakeTtsClient();
  let capturedMessages;
  const llmClient = makeFakeLlmClient((messages, { onToken, onDone }) => {
    capturedMessages = messages;
    onToken('Xin ');
    onToken('chào');
    onToken('.');
    onDone();
  });

  const controlMessages = [];
  const orchestrator = createSessionOrchestrator({
    systemPrompt: 'SYS',
    sttClient,
    llmClient,
    ttsClient,
    openingGreeting: 'GREET_INSTRUCTION',
    sendControl: (msg) => controlMessages.push(msg),
    sendAudio: () => {},
  });

  await orchestrator.start();
  await new Promise((r) => setImmediate(r));

  assert.ok(!controlMessages.some((m) => m.type === 'transcript'));
  assert.ok(controlMessages.some((m) => m.type === 'state' && m.state === 'speaking'));
  assert.deepEqual(ttsClient.calls.sendText, ['Xin chào.']);
  assert.equal(capturedMessages[0].role, 'system');
  const lastMsg = capturedMessages[capturedMessages.length - 1];
  assert.equal(lastMsg.role, 'user');
  assert.equal(lastMsg.content, 'GREET_INSTRUCTION');

  ttsClient.emitTurnComplete();

  sttClient.emitTranscriptFinal('Tôi muốn hỏi giá cước');
  await new Promise((r) => setImmediate(r));

  assert.equal(capturedMessages[1].role, 'assistant');
  assert.equal(capturedMessages[1].content, 'Xin chào.');
  assert.equal(capturedMessages[2].role, 'user');
  assert.equal(capturedMessages[2].content, 'Tôi muốn hỏi giá cước');
});

test('opening greeting: disabled by default (no openingGreeting option) does not speak on start()', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeFakeTtsClient();
  const llmClient = makeFakeLlmClient(() => {
    assert.fail('LLM should not be called on start() when openingGreeting is not set');
  });
  const orchestrator = createSessionOrchestrator({
    systemPrompt: 'SYS',
    sttClient,
    llmClient,
    ttsClient,
    sendControl: () => {},
    sendAudio: () => {},
  });

  await orchestrator.start();
  await new Promise((r) => setImmediate(r));

  assert.equal(ttsClient.calls.connect, 0);
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

test('TTS connect failure surfaces an error, aborts the parallel LLM call, and returns to listening instead of hanging in thinking', async () => {
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

  // The LLM call starts immediately, in parallel with ttsClient.connect() (fix #4) —
  // it does not wait for TTS to be ready.
  assert.equal(llmInvoked, true);

  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));

  // Since TTS can never deliver a response for this turn, the now-pointless LLM
  // call is aborted instead of being left running for nobody to hear (fix #3's
  // "no zombie requests" principle applied to this failure path too).
  assert.equal(llmClient.abortCalls.length, 1);
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

test('barge-in during TTS connect aborts the already-started LLM call and ignores the late TTS connect resolution', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeDeferredConnectTtsClient();
  let streamCompletionCalls = 0;
  const abortCalls = [];
  const llmClient = {
    streamCompletion: () => {
      streamCompletionCalls += 1;
      return { abort: () => abortCalls.push(true) };
    },
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

  // The LLM call starts immediately, in parallel with the still-pending TTS connect
  // (fix #4) — it is not nested inside ttsClient.connect().then().
  assert.equal(streamCompletionCalls, 1, 'LLM should start without waiting on TTS connect');

  sttClient.emitSpeechStarted(); // barge-in while still awaiting TTS connect

  assert.equal(abortCalls.length, 1, 'the in-flight LLM call for the superseded turn is aborted');

  ttsClient.resolveConnect(); // now let the superseded connect() settle
  await new Promise((r) => setImmediate(r));

  assert.equal(streamCompletionCalls, 1, 'no second LLM call is started for the superseded turn');
  assert.equal(ttsClient.calls.sendText.length, 0, 'nothing is sent to the now-irrelevant TTS connection');
  assert.ok(controlMessages.some((m) => m.type === 'interrupt'));
});

test('fix #4: LLM streamCompletion starts in parallel with ttsClient.connect(), not nested inside it', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeDeferredConnectTtsClient(); // connect() stays pending until resolveConnect()
  let streamCompletionCalledBeforeConnectResolved = false;

  const llmClient = {
    streamCompletion: (messages, callbacks) => {
      // At the instant this runs, ttsClient.connect() has been called but has not
      // resolved yet (deferred) — proves the LLM call isn't waiting on it.
      streamCompletionCalledBeforeConnectResolved = true;
      callbacks.onToken('Xin chào.'); // triggers a punctuation-boundary flush
      return { abort: () => {} };
    },
  };

  const orchestrator = createSessionOrchestrator({
    systemPrompt: 'SYS',
    sttClient,
    llmClient,
    ttsClient,
    sendControl: () => {},
    sendAudio: () => {},
  });

  await orchestrator.start();
  sttClient.emitTranscriptFinal('Chào bạn');

  assert.equal(streamCompletionCalledBeforeConnectResolved, true);
  assert.equal(ttsClient.calls.connect, 1);
  assert.deepEqual(ttsClient.calls.sendText, [], 'the flushed chunk must be queued locally, not sent before TTS is ready');

  ttsClient.resolveConnect();
  await new Promise((r) => setImmediate(r));

  assert.deepEqual(ttsClient.calls.sendText, ['Xin chào.'], 'the queued chunk is flushed in order once TTS connects');
});

test('fix #6: LLM error after TTS is connected speaks a Vietnamese apology instead of the raw error', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeFakeTtsClient();
  let capturedCallbacks;
  const llmClient = makeFakeLlmClient((messages, callbacks) => {
    capturedCallbacks = callbacks;
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
  await new Promise((r) => setImmediate(r)); // let ttsClient.connect() resolve -> ttsReady = true

  capturedCallbacks.onError(new Error('boom'));

  assert.deepEqual(ttsClient.calls.sendText, ['Xin lỗi, tôi gặp sự cố kỹ thuật, bạn nói lại được không?']);
  assert.equal(ttsClient.calls.endTurn, 1);
  assert.ok(
    !controlMessages.some((m) => m.type === 'error' && m.message === 'boom'),
    'the raw error message should not be shown to the user',
  );
  assert.ok(controlMessages.some((m) => m.type === 'state' && m.state === 'speaking'));
});

test('fix #6: LLM error while TTS is still connecting falls back to an error control message', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeDeferredConnectTtsClient();
  let capturedCallbacks;
  const llmClient = {
    streamCompletion: (messages, callbacks) => {
      capturedCallbacks = callbacks;
      return { abort: () => {} };
    },
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
  sttClient.emitTranscriptFinal('Chào bạn'); // TTS connect still pending

  capturedCallbacks.onError(new Error('boom'));

  assert.ok(controlMessages.some((m) => m.type === 'error' && m.message === 'boom'));
  assert.ok(controlMessages.some((m) => m.type === 'state' && m.state === 'listening'));
  assert.equal(ttsClient.calls.abort, 1);

  // The now-superseded TTS connect resolving later should be a no-op, not a crash.
  ttsClient.resolveConnect();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(ttsClient.calls.sendText, []);
});

test('fix #3: stop() during "thinking" aborts the in-flight LLM call and ignores a later TTS connect resolution', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeDeferredConnectTtsClient();
  const abortCalls = [];
  const llmClient = {
    streamCompletion: () => ({ abort: () => abortCalls.push(true) }),
  };

  const orchestrator = createSessionOrchestrator({
    systemPrompt: 'SYS',
    sttClient,
    llmClient,
    ttsClient,
    sendControl: () => {},
    sendAudio: () => {},
  });

  await orchestrator.start();
  sttClient.emitTranscriptFinal('Kể chuyện đi'); // state -> thinking; TTS connect still pending

  orchestrator.stop(); // browser hung up mid-"thinking"

  assert.equal(abortCalls.length, 1, 'the in-flight LLM call must be aborted on hangup');

  ttsClient.resolveConnect(); // late resolution after hangup must not start a new LLM request
  await new Promise((r) => setImmediate(r));

  assert.equal(ttsClient.calls.sendText.length, 0);
});

test('fix #1: sttClient disconnected sends an error control message and does not throw', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeFakeTtsClient();
  const llmClient = makeFakeLlmClient(() => {});

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

  assert.doesNotThrow(() => sttClient.emitDisconnected());
  assert.ok(
    controlMessages.some((m) => m.type === 'error' && m.message === 'Mất kết nối dịch vụ nhận diện giọng nói.'),
  );

  // Session should now be dormant rather than throwing on any further event.
  assert.doesNotThrow(() => sttClient.emitDisconnected());
});

test('fix #1: ttsClient disconnected while speaking sends an error and returns to listening', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeFakeTtsClient();
  const llmClient = makeFakeLlmClient((messages, { onToken }) => {
    onToken('Xin chào.'); // punctuation flush -> speaking, once TTS is ready
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

  assert.doesNotThrow(() => ttsClient.emitDisconnected());

  assert.ok(controlMessages.some((m) => m.type === 'error' && m.message === 'Mất kết nối dịch vụ giọng nói.'));
  assert.ok(controlMessages.filter((m) => m.type === 'state' && m.state === 'listening').length >= 1);
});

test('fix #1 (round 2): sttClient disconnected during an active turn abandons the turn instead of leaving it to crash/speak later', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeDeferredConnectTtsClient(); // TTS connect stays pending; a chunk gets queued
  const abortCalls = [];
  const llmClient = {
    streamCompletion: (messages, callbacks) => {
      callbacks.onToken('Xin chào.'); // flush -> queued, since TTS isn't ready yet
      return { abort: () => abortCalls.push(true) };
    },
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
  sttClient.emitTranscriptFinal('Kể chuyện đi'); // state -> thinking; chunk queued; TTS connect pending

  assert.doesNotThrow(() => sttClient.emitDisconnected());

  assert.equal(abortCalls.length, 1, 'the in-flight LLM call must be aborted');
  assert.equal(ttsClient.calls.abort, 1, 'the TTS turn must be aborted too, not left dangling');
  assert.ok(
    controlMessages.some((m) => m.type === 'error' && m.message === 'Mất kết nối dịch vụ nhận diện giọng nói.'),
  );

  // The now-irrelevant deferred TTS connect resolving afterward must be a no-op —
  // not a crash (state.transition('TTS_STARTED') from 'idle') and not the agent
  // speaking on a call that's already been declared over.
  assert.doesNotThrow(() => ttsClient.resolveConnect());
  await new Promise((r) => setImmediate(r));

  assert.equal(ttsClient.calls.sendText.length, 0);
});

test('fix #2 (round 2): ttsClient disconnected while still "thinking" (before any chunk flushed) recovers to listening instead of wedging forever', async () => {
  const sttClient = makeFakeSttClient();
  const ttsClient = makeFakeTtsClient(); // connect() resolves promptly
  const llmClient = makeFakeLlmClient(() => {}); // no tokens yet -> state stays 'thinking'

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
  await new Promise((r) => setImmediate(r)); // let ttsClient.connect() resolve; still 'thinking'

  assert.doesNotThrow(() => ttsClient.emitDisconnected());

  assert.ok(controlMessages.some((m) => m.type === 'error' && m.message === 'Mất kết nối dịch vụ giọng nói.'));
  assert.ok(controlMessages.some((m) => m.type === 'state' && m.state === 'listening'));

  // Session must be recoverable, not wedged in 'speaking'/'thinking' forever.
  assert.doesNotThrow(() => sttClient.emitTranscriptFinal('thử lại'));
  const transcriptMessages = controlMessages.filter((m) => m.type === 'transcript');
  assert.ok(transcriptMessages.some((m) => m.text === 'thử lại'), 'the next turn must actually start, proving the session is not wedged');
});
