// src/realtimeSttClient.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createRealtimeSttClient } = require('./realtimeSttClient');

class FakeWs extends EventEmitter {
  constructor() {
    super();
    this.sent = [];
    // Mirror the `ws` package's readyState constants/behavior: instances start
    // CONNECTING and only become OPEN once the 'open' event fires.
    this.readyState = FakeWs.CONNECTING;
  }
  send(data) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.closed = true;
  }
}
FakeWs.CONNECTING = 0;
FakeWs.OPEN = 1;
FakeWs.CLOSING = 2;
FakeWs.CLOSED = 3;
FakeWs.prototype.CONNECTING = FakeWs.CONNECTING;
FakeWs.prototype.OPEN = FakeWs.OPEN;
FakeWs.prototype.CLOSING = FakeWs.CLOSING;
FakeWs.prototype.CLOSED = FakeWs.CLOSED;

test('connect() opens the socket with the auth header and sends session.update', async () => {
  let capturedUrl;
  let capturedOptions;
  const fakeWs = new FakeWs();
  const client = createRealtimeSttClient({
    apiKey: 'sk-test',
    model: 'gpt-realtime-2.1',
    transcribeModel: 'gpt-4o-transcribe',
    createWebSocket: (url, options) => {
      capturedUrl = url;
      capturedOptions = options;
      queueMicrotask(() => {
        fakeWs.readyState = FakeWs.OPEN;
        fakeWs.emit('open');
      });
      return fakeWs;
    },
  });

  await client.connect();

  assert.equal(capturedUrl, 'wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1');
  assert.deepEqual(capturedOptions, { headers: { Authorization: 'Bearer sk-test' } });
  assert.equal(fakeWs.sent.length, 1);
  assert.equal(fakeWs.sent[0].type, 'session.update');
  assert.equal(fakeWs.sent[0].session.audio.input.turn_detection.type, 'server_vad');
  assert.equal(fakeWs.sent[0].session.audio.input.turn_detection.create_response, false);
  assert.equal(fakeWs.sent[0].session.audio.input.transcription.model, 'gpt-4o-transcribe');
  assert.equal(fakeWs.sent[0].session.audio.input.transcription.language, 'vi');
  assert.equal(fakeWs.sent[0].session.audio.input.noise_reduction.type, 'near_field');
});

test('emits speechStarted and transcriptFinal from incoming server events', async () => {
  const fakeWs = new FakeWs();
  const client = createRealtimeSttClient({
    apiKey: 'sk-test',
    model: 'gpt-realtime-2.1',
    transcribeModel: 'gpt-4o-transcribe',
    createWebSocket: () => {
      queueMicrotask(() => {
        fakeWs.readyState = FakeWs.OPEN;
        fakeWs.emit('open');
      });
      return fakeWs;
    },
  });
  await client.connect();

  const events = [];
  client.on('speechStarted', () => events.push(['speechStarted']));
  client.on('transcriptFinal', (t) => events.push(['transcriptFinal', t]));

  fakeWs.emit('message', Buffer.from(JSON.stringify({ type: 'input_audio_buffer.speech_started' })));
  fakeWs.emit('message', Buffer.from(JSON.stringify({
    type: 'conversation.item.input_audio_transcription.completed',
    transcript: 'Xin chào',
  })));

  assert.deepEqual(events, [['speechStarted'], ['transcriptFinal', 'Xin chào']]);
});

test('sendAudioChunk sends base64-encoded audio in an input_audio_buffer.append event', async () => {
  const fakeWs = new FakeWs();
  const client = createRealtimeSttClient({
    apiKey: 'sk-test',
    model: 'gpt-realtime-2.1',
    transcribeModel: 'gpt-4o-transcribe',
    createWebSocket: () => {
      queueMicrotask(() => {
        fakeWs.readyState = FakeWs.OPEN;
        fakeWs.emit('open');
      });
      return fakeWs;
    },
  });
  await client.connect();
  fakeWs.sent = [];

  client.sendAudioChunk(Buffer.from([1, 2, 3]));

  assert.equal(fakeWs.sent.length, 1);
  assert.equal(fakeWs.sent[0].type, 'input_audio_buffer.append');
  assert.equal(fakeWs.sent[0].audio, Buffer.from([1, 2, 3]).toString('base64'));
});

test('sendAudioChunk before the socket is open is a safe no-op', async () => {
  const fakeWs = new FakeWs();
  const client = createRealtimeSttClient({
    apiKey: 'sk-test',
    model: 'gpt-realtime-2.1',
    transcribeModel: 'gpt-4o-transcribe',
    createWebSocket: () => {
      // 'open' is deferred to a microtask, same as the real `ws` handshake, so
      // the socket is still CONNECTING when we call sendAudioChunk below.
      queueMicrotask(() => {
        fakeWs.readyState = FakeWs.OPEN;
        fakeWs.emit('open');
      });
      return fakeWs;
    },
  });

  const connectPromise = client.connect();
  assert.equal(fakeWs.readyState, FakeWs.CONNECTING);

  assert.doesNotThrow(() => client.sendAudioChunk(Buffer.from([1, 2, 3])));
  assert.equal(fakeWs.sent.length, 0);

  await connectPromise;
});

test('emits disconnected when the socket closes unexpectedly', async () => {
  const fakeWs = new FakeWs();
  const client = createRealtimeSttClient({
    apiKey: 'sk-test',
    model: 'gpt-realtime-2.1',
    transcribeModel: 'gpt-4o-transcribe',
    createWebSocket: () => {
      queueMicrotask(() => {
        fakeWs.readyState = FakeWs.OPEN;
        fakeWs.emit('open');
      });
      return fakeWs;
    },
  });
  await client.connect();

  let disconnected = false;
  client.on('disconnected', () => {
    disconnected = true;
  });

  fakeWs.emit('close');

  assert.equal(disconnected, true);
});

test('close() does not emit disconnected (deliberate shutdown, not a drop)', async () => {
  const fakeWs = new FakeWs();
  const client = createRealtimeSttClient({
    apiKey: 'sk-test',
    model: 'gpt-realtime-2.1',
    transcribeModel: 'gpt-4o-transcribe',
    createWebSocket: () => {
      queueMicrotask(() => {
        fakeWs.readyState = FakeWs.OPEN;
        fakeWs.emit('open');
      });
      return fakeWs;
    },
  });
  await client.connect();

  let disconnected = false;
  client.on('disconnected', () => {
    disconnected = true;
  });

  client.close();
  fakeWs.emit('close'); // real ws would emit this after close() tears the socket down

  assert.equal(disconnected, false);
});

test('fix #3 (round 2): a stale socket from a superseded connect() firing close late does not false-alarm, and a real drop on the new connection is still reported', async () => {
  const fakeWs1 = new FakeWs();
  const fakeWs2 = new FakeWs();
  const sockets = [fakeWs1, fakeWs2];
  let callIndex = 0;
  const client = createRealtimeSttClient({
    apiKey: 'sk-test',
    model: 'gpt-realtime-2.1',
    transcribeModel: 'gpt-4o-transcribe',
    createWebSocket: () => {
      const socket = sockets[callIndex];
      callIndex += 1;
      queueMicrotask(() => {
        socket.readyState = FakeWs.OPEN;
        socket.emit('open');
      });
      return socket;
    },
  });

  await client.connect(); // first connection -> fakeWs1
  await client.connect(); // reconnect -> fakeWs2; `ws` is reassigned and expectClose resets to false

  let disconnected = false;
  client.on('disconnected', () => {
    disconnected = true;
  });

  // fakeWs1's now-orphaned close event arrives late, after the reconnect.
  fakeWs1.emit('close');
  assert.equal(disconnected, false, 'an orphaned previous-connection socket closing must not false-alarm on the new connection');

  // The live (second) socket dropping unexpectedly must still be reported.
  fakeWs2.emit('close');
  assert.equal(disconnected, true, 'a real drop on the current connection must still be reported after a reconnect');
});

test('close() closes the underlying socket', async () => {
  const fakeWs = new FakeWs();
  const client = createRealtimeSttClient({
    apiKey: 'sk-test',
    model: 'gpt-realtime-2.1',
    transcribeModel: 'gpt-4o-transcribe',
    createWebSocket: () => {
      queueMicrotask(() => {
        fakeWs.readyState = FakeWs.OPEN;
        fakeWs.emit('open');
      });
      return fakeWs;
    },
  });
  await client.connect();
  client.close();
  assert.equal(fakeWs.closed, true);
});
