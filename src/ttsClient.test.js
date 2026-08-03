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
