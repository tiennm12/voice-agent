const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createTtsClient } = require('./ttsClient');

class FakeWs extends EventEmitter {
  constructor() {
    super();
    this.sent = [];
  }
  send(data) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.closed = true;
  }
}

test('connect() opens the socket with the right URL/headers and sends the init message', async () => {
  let capturedUrl;
  let capturedOptions;
  const fakeWs = new FakeWs();
  const client = createTtsClient({
    apiKey: 'xi-test',
    voiceId: 'voice123',
    modelId: 'eleven_flash_v2_5',
    createWebSocket: (url, options) => {
      capturedUrl = url;
      capturedOptions = options;
      queueMicrotask(() => fakeWs.emit('open'));
      return fakeWs;
    },
  });

  await client.connect();

  assert.equal(
    capturedUrl,
    'wss://api.elevenlabs.io/v1/text-to-speech/voice123/stream-input?model_id=eleven_flash_v2_5&output_format=mp3_44100_128',
  );
  assert.deepEqual(capturedOptions, { headers: { 'xi-api-key': 'xi-test' } });
  assert.equal(fakeWs.sent[0].text, ' ');
});

test('sendText sends a text message and endTurn sends an empty-text close message', async () => {
  const fakeWs = new FakeWs();
  const client = createTtsClient({
    apiKey: 'xi-test',
    voiceId: 'voice123',
    modelId: 'eleven_flash_v2_5',
    createWebSocket: () => {
      queueMicrotask(() => fakeWs.emit('open'));
      return fakeWs;
    },
  });
  await client.connect();
  fakeWs.sent = [];

  client.sendText('Xin chào.');
  client.endTurn();

  assert.deepEqual(fakeWs.sent, [{ text: 'Xin chào. ' }, { text: '' }]);
});

test('emits audioChunk for messages with audio, turnComplete when isFinal', async () => {
  const fakeWs = new FakeWs();
  const client = createTtsClient({
    apiKey: 'xi-test',
    voiceId: 'voice123',
    modelId: 'eleven_flash_v2_5',
    createWebSocket: () => {
      queueMicrotask(() => fakeWs.emit('open'));
      return fakeWs;
    },
  });
  await client.connect();

  const chunks = [];
  let turnComplete = false;
  client.on('audioChunk', (buf) => chunks.push(buf));
  client.on('turnComplete', () => {
    turnComplete = true;
  });

  const audioB64 = Buffer.from([9, 9, 9]).toString('base64');
  fakeWs.emit('message', Buffer.from(JSON.stringify({ audio: audioB64 })));
  fakeWs.emit('message', Buffer.from(JSON.stringify({ isFinal: true })));

  assert.equal(chunks.length, 1);
  assert.deepEqual(chunks[0], Buffer.from([9, 9, 9]));
  assert.equal(turnComplete, true);
});

test('abort() closes the socket immediately', async () => {
  const fakeWs = new FakeWs();
  const client = createTtsClient({
    apiKey: 'xi-test',
    voiceId: 'voice123',
    modelId: 'eleven_flash_v2_5',
    createWebSocket: () => {
      queueMicrotask(() => fakeWs.emit('open'));
      return fakeWs;
    },
  });
  await client.connect();
  client.abort();
  assert.equal(fakeWs.closed, true);
});

test('emits disconnected when the socket closes unexpectedly mid-turn', async () => {
  const fakeWs = new FakeWs();
  const client = createTtsClient({
    apiKey: 'xi-test',
    voiceId: 'voice123',
    modelId: 'eleven_flash_v2_5',
    createWebSocket: () => {
      queueMicrotask(() => fakeWs.emit('open'));
      return fakeWs;
    },
  });
  await client.connect();

  let disconnected = false;
  client.on('disconnected', () => {
    disconnected = true;
  });

  fakeWs.emit('close'); // no isFinal message and no abort() first -> unexpected

  assert.equal(disconnected, true);
});

test('a normal end-of-turn close (isFinal then server closes) does not emit disconnected', async () => {
  const fakeWs = new FakeWs();
  const client = createTtsClient({
    apiKey: 'xi-test',
    voiceId: 'voice123',
    modelId: 'eleven_flash_v2_5',
    createWebSocket: () => {
      queueMicrotask(() => fakeWs.emit('open'));
      return fakeWs;
    },
  });
  await client.connect();

  let disconnected = false;
  client.on('disconnected', () => {
    disconnected = true;
  });

  fakeWs.emit('message', Buffer.from(JSON.stringify({ isFinal: true })));
  fakeWs.emit('close'); // server closes the socket after the final message, as expected

  assert.equal(disconnected, false);
});

test('abort() does not emit disconnected (deliberate shutdown, not a drop)', async () => {
  const fakeWs = new FakeWs();
  const client = createTtsClient({
    apiKey: 'xi-test',
    voiceId: 'voice123',
    modelId: 'eleven_flash_v2_5',
    createWebSocket: () => {
      queueMicrotask(() => fakeWs.emit('open'));
      return fakeWs;
    },
  });
  await client.connect();

  let disconnected = false;
  client.on('disconnected', () => {
    disconnected = true;
  });

  client.abort();
  fakeWs.emit('close'); // real ws would emit this after abort() tears the socket down

  assert.equal(disconnected, false);
});

test('fix #3 (round 2): a stale socket from a superseded connect() firing close late does not false-alarm, and a real drop on the new connection is still reported', async () => {
  const fakeWs1 = new FakeWs();
  const fakeWs2 = new FakeWs();
  const sockets = [fakeWs1, fakeWs2];
  let callIndex = 0;
  const client = createTtsClient({
    apiKey: 'xi-test',
    voiceId: 'voice123',
    modelId: 'eleven_flash_v2_5',
    createWebSocket: () => {
      const socket = sockets[callIndex];
      callIndex += 1;
      queueMicrotask(() => socket.emit('open'));
      return socket;
    },
  });

  await client.connect(); // turn 1 -> fakeWs1
  fakeWs1.emit('message', Buffer.from(JSON.stringify({ isFinal: true }))); // turn 1 ends normally

  await client.connect(); // turn 2 -> fakeWs2; `ws` is reassigned and expectClose resets to false

  let disconnected = false;
  client.on('disconnected', () => {
    disconnected = true;
  });

  // Turn 1's now-orphaned socket finally fires its close event, arriving late (after
  // turn 2 already reconnected). This must not be reported as a drop on turn 2, and
  // must not consume/re-latch the flag meant for turn 2's own live socket.
  fakeWs1.emit('close');
  assert.equal(disconnected, false, 'an orphaned previous-turn socket closing must not false-alarm on the new connection');

  // The live (turn 2) socket dropping unexpectedly must still be reported.
  fakeWs2.emit('close');
  assert.equal(disconnected, true, 'a real drop on the current connection must still be reported after a reconnect');
});
