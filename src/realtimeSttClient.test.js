// src/realtimeSttClient.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createRealtimeSttClient } = require('./realtimeSttClient');

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
      queueMicrotask(() => fakeWs.emit('open'));
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
});

test('emits speechStarted and transcriptFinal from incoming server events', async () => {
  const fakeWs = new FakeWs();
  const client = createRealtimeSttClient({
    apiKey: 'sk-test',
    model: 'gpt-realtime-2.1',
    transcribeModel: 'gpt-4o-transcribe',
    createWebSocket: () => {
      queueMicrotask(() => fakeWs.emit('open'));
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
      queueMicrotask(() => fakeWs.emit('open'));
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

test('close() closes the underlying socket', async () => {
  const fakeWs = new FakeWs();
  const client = createRealtimeSttClient({
    apiKey: 'sk-test',
    model: 'gpt-realtime-2.1',
    transcribeModel: 'gpt-4o-transcribe',
    createWebSocket: () => {
      queueMicrotask(() => fakeWs.emit('open'));
      return fakeWs;
    },
  });
  await client.connect();
  client.close();
  assert.equal(fakeWs.closed, true);
});
