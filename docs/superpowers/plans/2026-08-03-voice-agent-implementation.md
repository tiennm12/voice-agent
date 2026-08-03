# Voice Agent (Vietnamese) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a low-latency Vietnamese voice agent: browser mic → Node.js backend relay → OpenAI Realtime API (STT only) → GPT-4.1-mini (streaming) → ElevenLabs (streaming TTS) → browser playback, with barge-in support.

**Architecture:** One Node.js process serves the static frontend and terminates a WebSocket per call. The backend holds a persistent WS to OpenAI Realtime (transcription + server VAD, no auto response), streams GPT-4.1-mini tokens chunked at punctuation boundaries straight into an ElevenLabs streaming-input WS, and forwards each returned audio chunk to the browser immediately. A small state machine tracks call phase and drives barge-in (abort in-flight LLM/TTS when the user starts talking).

**Tech Stack:** Node.js ≥18 (built-in `node:test` runner, no test framework dependency), `ws` for all WebSocket connections, `openai` SDK for GPT-4.1-mini, plain HTML/CSS/JS + Web Audio (AudioWorklet) + MediaSource Extensions for the frontend (no frontend framework/bundler).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-03-voice-agent-design.md` — every task below implements a section of it.
- 1 call at a time (no multi-session scaling in this plan).
- Turn detection: OpenAI Realtime server VAD (`turn_detection: server_vad`), not push-to-talk.
- Barge-in required: user speech mid-response must abort the in-flight LLM + TTS turn.
- TTS model: ElevenLabs `eleven_flash_v2_5` (env-overridable).
- System prompt document is a placeholder file (`config/systemDoc.md`) — content filled in later, not part of this plan.
- No automated tests may call real OpenAI/ElevenLabs APIs — all network-touching modules must accept injected factories/clients so tests use fakes.
- Node's built-in test runner (`node --test`) is the only test tool — do not add jest/mocha/etc.

---

## File Structure

```
package.json
.env.example
.gitignore
README.md
config/
  systemDoc.md
src/
  config.js               config.test.js
  promptBuilder.js         promptBuilder.test.js
  textChunker.js            textChunker.test.js
  sessionState.js            sessionState.test.js
  realtimeSttClient.js        realtimeSttClient.test.js
  llmClient.js                 llmClient.test.js
  ttsClient.js                  ttsClient.test.js
  sessionOrchestrator.js         sessionOrchestrator.test.js
  server.js
public/
  index.html
  client.js
  pcm-encoder-processor.js
```

Each `src/*.js` module has one responsibility and is injectable (no module reaches for `process.env` or constructs a real `WebSocket`/`OpenAI` client internally except at the wiring point in `server.js`). This is what makes the orchestration logic testable without live API calls.

---

### Task 1: Project scaffold

**Files:**
- Create: `package.json`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `config/systemDoc.md`

**Interfaces:**
- Produces: `npm install`, `npm test`, `npm start` scripts that every later task relies on.

- [ ] **Step 1: Create `package.json`**

```json
{
  "name": "voice-agent",
  "version": "0.1.0",
  "private": true,
  "engines": {
    "node": ">=18.0.0"
  },
  "scripts": {
    "start": "node src/server.js",
    "test": "node --test src"
  },
  "dependencies": {
    "ws": "^8.18.0",
    "openai": "^4.68.0",
    "dotenv": "^16.4.5"
  }
}
```

- [ ] **Step 2: Create `.env.example`**

```
OPENAI_API_KEY=sk-...
OPENAI_REALTIME_MODEL=gpt-realtime-2.1
OPENAI_TRANSCRIBE_MODEL=gpt-4o-transcribe
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
ELEVENLABS_MODEL_ID=eleven_flash_v2_5
PORT=3000
```

- [ ] **Step 3: Create `.gitignore`**

```
node_modules/
.env
```

- [ ] **Step 4: Create `config/systemDoc.md` (placeholder document)**

```markdown
<!-- Placeholder: thay nội dung này bằng tài liệu tham khảo thật cho agent. -->

Bạn là trợ lý hỗ trợ khách hàng qua điện thoại của công ty. Trả lời ngắn gọn,
thân thiện, đúng trọng tâm câu hỏi.
```

- [ ] **Step 5: Install dependencies and verify**

Run: `npm install`
Expected: installs without error, creates `node_modules/` and `package-lock.json`.

Run: `node -e "console.log('scaffold ok')"`
Expected: prints `scaffold ok`.

- [ ] **Step 6: Commit**

```bash
git add package.json package-lock.json .env.example .gitignore config/systemDoc.md
git commit -m "chore: project scaffold"
```

---

### Task 2: Config loader + system prompt builder

**Files:**
- Create: `src/config.js`
- Test: `src/config.test.js`

**Interfaces:**
- Consumes: nothing (leaf module besides `node:fs`/`node:path`).
- Produces:
  - `buildSystemPrompt(documentText: string): string`
  - `loadConfig({ env?, readSystemDoc? }): { openaiApiKey, openaiRealtimeModel, openaiTranscribeModel, elevenLabsApiKey, elevenLabsVoiceId, elevenLabsModelId, port, systemPrompt }`
  - `loadConfig` throws `Error` if `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, or `ELEVENLABS_VOICE_ID` is missing from `env`.

- [ ] **Step 1: Write the failing test**

```javascript
// src/config.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildSystemPrompt, loadConfig } = require('./config');

test('buildSystemPrompt embeds the trimmed document text', () => {
  const prompt = buildSystemPrompt('  Giờ mở cửa: 8h-17h.  ');
  assert.match(prompt, /Giờ mở cửa: 8h-17h\./);
  assert.match(prompt, /tiếng Việt/);
});

test('loadConfig throws when a required env var is missing', () => {
  assert.throws(
    () => loadConfig({ env: {}, readSystemDoc: () => 'doc' }),
    /OPENAI_API_KEY/,
  );
});

test('loadConfig applies defaults for optional env vars', () => {
  const config = loadConfig({
    env: {
      OPENAI_API_KEY: 'k1',
      ELEVENLABS_API_KEY: 'k2',
      ELEVENLABS_VOICE_ID: 'v1',
    },
    readSystemDoc: () => 'doc text',
  });
  assert.equal(config.openaiRealtimeModel, 'gpt-realtime-2.1');
  assert.equal(config.openaiTranscribeModel, 'gpt-4o-transcribe');
  assert.equal(config.elevenLabsModelId, 'eleven_flash_v2_5');
  assert.equal(config.port, 3000);
  assert.match(config.systemPrompt, /doc text/);
});

test('loadConfig uses the injected readSystemDoc function', () => {
  let called = false;
  loadConfig({
    env: {
      OPENAI_API_KEY: 'k1',
      ELEVENLABS_API_KEY: 'k2',
      ELEVENLABS_VOICE_ID: 'v1',
    },
    readSystemDoc: () => {
      called = true;
      return 'doc';
    },
  });
  assert.equal(called, true);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test src/config.test.js`
Expected: FAIL — `Cannot find module './config'`.

- [ ] **Step 3: Write minimal implementation**

```javascript
// src/config.js
const fs = require('node:fs');
const path = require('node:path');

function buildSystemPrompt(documentText) {
  return [
    'Bạn là trợ lý gọi điện thoại, trả lời bằng tiếng Việt, ngắn gọn, tự nhiên như đang nói chuyện.',
    'Dựa vào tài liệu tham khảo dưới đây để trả lời; nếu không chắc chắn, nói rõ là bạn không có thông tin đó, không bịa.',
    '---',
    documentText.trim(),
    '---',
  ].join('\n');
}

function defaultReadSystemDoc() {
  return fs.readFileSync(path.join(__dirname, '..', 'config', 'systemDoc.md'), 'utf8');
}

function loadConfig({ env = process.env, readSystemDoc = defaultReadSystemDoc } = {}) {
  const required = ['OPENAI_API_KEY', 'ELEVENLABS_API_KEY', 'ELEVENLABS_VOICE_ID'];
  for (const key of required) {
    if (!env[key]) {
      throw new Error(`Missing required env var: ${key}`);
    }
  }

  return {
    openaiApiKey: env.OPENAI_API_KEY,
    openaiRealtimeModel: env.OPENAI_REALTIME_MODEL || 'gpt-realtime-2.1',
    openaiTranscribeModel: env.OPENAI_TRANSCRIBE_MODEL || 'gpt-4o-transcribe',
    elevenLabsApiKey: env.ELEVENLABS_API_KEY,
    elevenLabsVoiceId: env.ELEVENLABS_VOICE_ID,
    elevenLabsModelId: env.ELEVENLABS_MODEL_ID || 'eleven_flash_v2_5',
    port: Number(env.PORT) || 3000,
    systemPrompt: buildSystemPrompt(readSystemDoc()),
  };
}

module.exports = { buildSystemPrompt, loadConfig };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test src/config.test.js`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/config.js src/config.test.js
git commit -m "feat: add config loader and system prompt builder"
```

---

### Task 3: Prompt builder (chat message assembly)

**Files:**
- Create: `src/promptBuilder.js`
- Test: `src/promptBuilder.test.js`

**Interfaces:**
- Produces: `buildMessages({ systemPrompt: string, history: Array<{role: 'user'|'assistant', content: string}>, newUserText: string }): Array<{role, content}>`

- [ ] **Step 1: Write the failing test**

```javascript
// src/promptBuilder.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildMessages } = require('./promptBuilder');

test('puts the system prompt first, then history, then the new user turn', () => {
  const messages = buildMessages({
    systemPrompt: 'SYS',
    history: [
      { role: 'user', content: 'Chào' },
      { role: 'assistant', content: 'Chào bạn' },
    ],
    newUserText: 'Giờ mở cửa lúc mấy giờ?',
  });

  assert.deepEqual(messages, [
    { role: 'system', content: 'SYS' },
    { role: 'user', content: 'Chào' },
    { role: 'assistant', content: 'Chào bạn' },
    { role: 'user', content: 'Giờ mở cửa lúc mấy giờ?' },
  ]);
});

test('works with empty history', () => {
  const messages = buildMessages({ systemPrompt: 'SYS', history: [], newUserText: 'Hi' });
  assert.deepEqual(messages, [
    { role: 'system', content: 'SYS' },
    { role: 'user', content: 'Hi' },
  ]);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test src/promptBuilder.test.js`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```javascript
// src/promptBuilder.js
function buildMessages({ systemPrompt, history, newUserText }) {
  return [
    { role: 'system', content: systemPrompt },
    ...history,
    { role: 'user', content: newUserText },
  ];
}

module.exports = { buildMessages };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test src/promptBuilder.test.js`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add src/promptBuilder.js src/promptBuilder.test.js
git commit -m "feat: add chat message builder"
```

---

### Task 4: Text chunker (streaming token → TTS-ready chunk)

This is the core latency-critical piece from the spec's data-flow step 4: split the
GPT-4.1-mini token stream into flush-ready chunks at punctuation boundaries, falling
back to a word-count boundary so a long clause without punctuation still gets flushed
promptly.

**Files:**
- Create: `src/textChunker.js`
- Test: `src/textChunker.test.js`

**Interfaces:**
- Produces: `createTextChunker({ onFlush: (chunkText: string) => void, maxWordsBeforeFlush?: number }): { push(deltaText: string): void, end(): void }`

- [ ] **Step 1: Write the failing test**

```javascript
// src/textChunker.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { createTextChunker } = require('./textChunker');

test('flushes a chunk when punctuation arrives, split across multiple pushes', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c) });
  chunker.push('Xin ');
  chunker.push('chào');
  chunker.push('.');
  chunker.push(' Bạn');
  assert.deepEqual(flushed, ['Xin chào.']);
});

test('flushes multiple punctuation-bounded chunks from one push', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c) });
  chunker.push('Chào bạn, hôm nay trời đẹp. Bạn khỏe không?');
  assert.deepEqual(flushed, ['Chào bạn,', 'hôm nay trời đẹp.', 'Bạn khỏe không?']);
});

test('falls back to a word-count flush when no punctuation appears', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c), maxWordsBeforeFlush: 4 });
  chunker.push('một hai ba bốn năm sáu');
  assert.deepEqual(flushed, ['một hai ba bốn']);
});

test('end() flushes any remaining buffered text', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c) });
  chunker.push('không có dấu câu');
  chunker.end();
  assert.deepEqual(flushed, ['không có dấu câu']);
});

test('end() does not flush an empty or whitespace-only buffer', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c) });
  chunker.push('   ');
  chunker.end();
  assert.deepEqual(flushed, []);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test src/textChunker.test.js`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```javascript
// src/textChunker.js
const PUNCTUATION = /[.!?;:,]/;

function wordCount(text) {
  const trimmed = text.trim();
  return trimmed === '' ? 0 : trimmed.split(/\s+/).length;
}

function createTextChunker({ onFlush, maxWordsBeforeFlush = 8 }) {
  let buffer = '';

  function push(deltaText) {
    buffer += deltaText;

    let progressed = true;
    while (progressed) {
      progressed = false;

      const match = buffer.match(PUNCTUATION);
      if (match) {
        const idx = match.index;
        const chunk = buffer.slice(0, idx + 1).trim();
        buffer = buffer.slice(idx + 1);
        if (chunk.length > 0) onFlush(chunk);
        progressed = true;
        continue;
      }

      if (wordCount(buffer) > maxWordsBeforeFlush) {
        const trimmed = buffer.trim();
        const lastSpace = trimmed.lastIndexOf(' ');
        if (lastSpace === -1) continue; // one very long word, wait for more input
        const chunk = trimmed.slice(0, lastSpace).trim();
        buffer = trimmed.slice(lastSpace + 1);
        if (chunk.length > 0) onFlush(chunk);
        progressed = true;
      }
    }
  }

  function end() {
    const remainder = buffer.trim();
    buffer = '';
    if (remainder.length > 0) onFlush(remainder);
  }

  return { push, end };
}

module.exports = { createTextChunker };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test src/textChunker.test.js`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/textChunker.js src/textChunker.test.js
git commit -m "feat: add streaming text chunker for TTS flushing"
```

---

### Task 5: Session state machine

**Files:**
- Create: `src/sessionState.js`
- Test: `src/sessionState.test.js`

**Interfaces:**
- Produces: `createSessionState({ onTransition?: (nextState, event) => void }): { current: string (getter), transition(event: string): string }`
- States: `'idle' | 'listening' | 'thinking' | 'speaking'`
- Events: `'CALL_STARTED' | 'TRANSCRIPT_FINAL' | 'TTS_STARTED' | 'TTS_ENDED' | 'USER_INTERRUPTED' | 'CALL_ENDED'`
- `CALL_ENDED` is valid from any state and always goes to `'idle'`. Any other invalid `(state, event)` pair throws `Error`.

- [ ] **Step 1: Write the failing test**

```javascript
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test src/sessionState.test.js`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```javascript
// src/sessionState.js
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test src/sessionState.test.js`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add src/sessionState.js src/sessionState.test.js
git commit -m "feat: add call session state machine"
```

---

### Task 6: Realtime STT client (OpenAI Realtime API wrapper)

Wraps the WebSocket connection to OpenAI Realtime API in transcription mode with
server VAD and `create_response: false` (so Realtime never auto-generates a spoken
reply — we only use it for speech-to-text and turn detection).

**Files:**
- Create: `src/realtimeSttClient.js`
- Test: `src/realtimeSttClient.test.js`

**Interfaces:**
- Produces: `createRealtimeSttClient({ apiKey, model, transcribeModel, createWebSocket? }): { connect(): Promise<void>, sendAudioChunk(buffer: Buffer): void, close(): void, on(event, handler): void }`
- Emits: `'speechStarted'` (no payload), `'transcriptFinal'` (payload: `string`), `'error'` (payload: `Error`)
- `createWebSocket` defaults to `(url, options) => new (require('ws'))(url, options)`; tests inject a fake.

- [ ] **Step 1: Write the failing test**

```javascript
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test src/realtimeSttClient.test.js`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```javascript
// src/realtimeSttClient.js
const { EventEmitter } = require('node:events');

function defaultCreateWebSocket(url, options) {
  const WebSocket = require('ws');
  return new WebSocket(url, options);
}

function createRealtimeSttClient({ apiKey, model, transcribeModel, createWebSocket = defaultCreateWebSocket }) {
  const emitter = new EventEmitter();
  let ws = null;

  function connect() {
    return new Promise((resolve, reject) => {
      ws = createWebSocket(`wss://api.openai.com/v1/realtime?model=${model}`, {
        headers: { Authorization: `Bearer ${apiKey}` },
      });

      ws.on('open', () => {
        ws.send(JSON.stringify({
          type: 'session.update',
          session: {
            type: 'realtime',
            model,
            output_modalities: ['text'],
            audio: {
              input: {
                format: { type: 'audio/pcm', rate: 24000 },
                turn_detection: {
                  type: 'server_vad',
                  create_response: false,
                  interrupt_response: false,
                },
                transcription: { model: transcribeModel },
              },
            },
          },
        }));
        resolve();
      });

      ws.on('message', (data) => {
        let event;
        try {
          event = JSON.parse(data.toString());
        } catch (err) {
          emitter.emit('error', err);
          return;
        }
        if (event.type === 'input_audio_buffer.speech_started') {
          emitter.emit('speechStarted');
        } else if (event.type === 'conversation.item.input_audio_transcription.completed') {
          emitter.emit('transcriptFinal', event.transcript);
        } else if (event.type === 'error') {
          emitter.emit('error', new Error((event.error && event.error.message) || 'Realtime API error'));
        }
      });

      ws.on('error', (err) => {
        emitter.emit('error', err);
        reject(err);
      });
    });
  }

  function sendAudioChunk(buffer) {
    if (!ws) return;
    ws.send(JSON.stringify({ type: 'input_audio_buffer.append', audio: buffer.toString('base64') }));
  }

  function close() {
    if (ws) {
      ws.close();
      ws = null;
    }
  }

  return { connect, sendAudioChunk, close, on: emitter.on.bind(emitter) };
}

module.exports = { createRealtimeSttClient };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test src/realtimeSttClient.test.js`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/realtimeSttClient.js src/realtimeSttClient.test.js
git commit -m "feat: add OpenAI Realtime STT client wrapper"
```

---

### Task 7: LLM client (GPT-4.1-mini streaming wrapper)

**Files:**
- Create: `src/llmClient.js`
- Test: `src/llmClient.test.js`

**Interfaces:**
- Produces: `createLlmClient({ openaiClient }): { streamCompletion(messages, { model?, onToken, onDone, onError }): { abort(): void } }`
- `openaiClient` must expose `chat.completions.create(params, options)`. Production wiring passes the real `openai` SDK client; tests inject a fake with a stubbed `create`.

- [ ] **Step 1: Write the failing test**

```javascript
// src/llmClient.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { createLlmClient } = require('./llmClient');

function chunk(content) {
  return { choices: [{ delta: { content } }] };
}

test('streamCompletion calls onToken for each content delta, then onDone', async () => {
  const fakeOpenaiClient = {
    chat: {
      completions: {
        async create() {
          return (async function* () {
            yield chunk('Xin ');
            yield chunk('chào');
            yield { choices: [{ delta: {} }] }; // e.g. a role-only delta chunk
          })();
        },
      },
    },
  };

  const llmClient = createLlmClient({ openaiClient: fakeOpenaiClient });
  const tokens = [];
  let done = false;

  await new Promise((resolve) => {
    llmClient.streamCompletion([{ role: 'user', content: 'hi' }], {
      onToken: (t) => tokens.push(t),
      onDone: () => {
        done = true;
        resolve();
      },
      onError: () => resolve(),
    });
  });

  assert.deepEqual(tokens, ['Xin ', 'chào']);
  assert.equal(done, true);
});

test('abort() stops the stream before completion and does not call onDone/onError', async () => {
  let sawSignal;
  const fakeOpenaiClient = {
    chat: {
      completions: {
        async create(params, options) {
          sawSignal = options.signal;
          return (async function* () {
            for (const token of ['a', 'b', 'c']) {
              if (options.signal.aborted) {
                const err = new Error('aborted');
                err.name = 'AbortError';
                throw err;
              }
              yield chunk(token);
              await new Promise((r) => setImmediate(r));
            }
          })();
        },
      },
    },
  };

  const llmClient = createLlmClient({ openaiClient: fakeOpenaiClient });
  const tokens = [];
  let doneCalled = false;
  let errorCalled = false;

  const handle = llmClient.streamCompletion([{ role: 'user', content: 'hi' }], {
    onToken: (t) => {
      tokens.push(t);
      if (tokens.length === 1) handle.abort();
    },
    onDone: () => {
      doneCalled = true;
    },
    onError: () => {
      errorCalled = true;
    },
  });

  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.ok(sawSignal);
  assert.equal(sawSignal.aborted, true);
  assert.equal(doneCalled, false);
  assert.equal(errorCalled, false);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test src/llmClient.test.js`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```javascript
// src/llmClient.js
function createLlmClient({ openaiClient }) {
  function streamCompletion(messages, { model = 'gpt-4.1-mini', onToken, onDone, onError }) {
    const controller = new AbortController();

    (async () => {
      try {
        const stream = await openaiClient.chat.completions.create(
          { model, messages, stream: true },
          { signal: controller.signal },
        );
        for await (const chunk of stream) {
          const token = chunk.choices && chunk.choices[0] && chunk.choices[0].delta && chunk.choices[0].delta.content;
          if (token) onToken(token);
        }
        onDone();
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        onError(err);
      }
    })();

    return { abort: () => controller.abort() };
  }

  return { streamCompletion };
}

module.exports = { createLlmClient };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test src/llmClient.test.js`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add src/llmClient.js src/llmClient.test.js
git commit -m "feat: add GPT-4.1-mini streaming client wrapper"
```

---

### Task 8: TTS client (ElevenLabs streaming-input WS wrapper)

**Files:**
- Create: `src/ttsClient.js`
- Test: `src/ttsClient.test.js`

**Interfaces:**
- Produces: `createTtsClient({ apiKey, voiceId, modelId, createWebSocket? }): { connect(): Promise<void>, sendText(text: string): void, endTurn(): void, abort(): void, on(event, handler): void }`
- Emits: `'audioChunk'` (payload: `Buffer`, raw mp3 bytes), `'turnComplete'` (no payload), `'error'` (payload: `Error`)

- [ ] **Step 1: Write the failing test**

```javascript
// src/ttsClient.test.js
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test src/ttsClient.test.js`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```javascript
// src/ttsClient.js
const { EventEmitter } = require('node:events');

function defaultCreateWebSocket(url, options) {
  const WebSocket = require('ws');
  return new WebSocket(url, options);
}

function createTtsClient({ apiKey, voiceId, modelId, createWebSocket = defaultCreateWebSocket }) {
  const emitter = new EventEmitter();
  let ws = null;

  function connect() {
    return new Promise((resolve, reject) => {
      const url = `wss://api.elevenlabs.io/v1/text-to-speech/${voiceId}/stream-input?model_id=${modelId}&output_format=mp3_44100_128`;
      ws = createWebSocket(url, { headers: { 'xi-api-key': apiKey } });

      ws.on('open', () => {
        ws.send(JSON.stringify({
          text: ' ',
          voice_settings: { stability: 0.5, similarity_boost: 0.75 },
        }));
        resolve();
      });

      ws.on('message', (data) => {
        let event;
        try {
          event = JSON.parse(data.toString());
        } catch (err) {
          emitter.emit('error', err);
          return;
        }
        if (event.audio) {
          emitter.emit('audioChunk', Buffer.from(event.audio, 'base64'));
        }
        if (event.isFinal) {
          emitter.emit('turnComplete');
        }
      });

      ws.on('error', (err) => {
        emitter.emit('error', err);
        reject(err);
      });
    });
  }

  function sendText(text) {
    if (!ws) return;
    ws.send(JSON.stringify({ text: `${text} ` }));
  }

  function endTurn() {
    if (!ws) return;
    ws.send(JSON.stringify({ text: '' }));
  }

  function abort() {
    if (ws) {
      ws.close();
      ws = null;
    }
  }

  return { connect, sendText, endTurn, abort, on: emitter.on.bind(emitter) };
}

module.exports = { createTtsClient };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test src/ttsClient.test.js`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/ttsClient.js src/ttsClient.test.js
git commit -m "feat: add ElevenLabs streaming TTS client wrapper"
```

---

### Task 9: Session orchestrator

Wires `sessionState` + `realtimeSttClient` + `llmClient` + `ttsClient` + `textChunker`
+ `promptBuilder` together for one call, implementing the spec's data flow and
barge-in logic. This is the task where the whole pipeline's behavior is actually
tested — with fakes for all three external clients.

**Files:**
- Create: `src/sessionOrchestrator.js`
- Test: `src/sessionOrchestrator.test.js`

**Interfaces:**
- Consumes:
  - `sttClient`: `{ connect(), sendAudioChunk(buf), close(), on(event, handler) }` (Task 6 shape)
  - `llmClient`: `{ streamCompletion(messages, { onToken, onDone, onError }): { abort() } }` (Task 7 shape)
  - `ttsClient`: `{ connect(), sendText(text), endTurn(), abort(), on(event, handler) }` (Task 8 shape)
  - `createTextChunker` (Task 4), `buildMessages` (Task 3), `createSessionState` (Task 5)
- Produces: `createSessionOrchestrator({ systemPrompt, sttClient, llmClient, ttsClient, sendControl, sendAudio }): { start(): Promise<void>, stop(): void, handleAudioChunk(buffer: Buffer): void }`
  - `sendControl(obj)`: sends a JSON control message to the browser (`{type:'transcript'|'state'|'interrupt'|'error', ...}`)
  - `sendAudio(buffer)`: sends a raw binary audio chunk to the browser

- [ ] **Step 1: Write the failing test**

```javascript
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test src/sessionOrchestrator.test.js`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```javascript
// src/sessionOrchestrator.js
const { createSessionState } = require('./sessionState');
const { createTextChunker } = require('./textChunker');
const { buildMessages } = require('./promptBuilder');

function createSessionOrchestrator({ systemPrompt, sttClient, llmClient, ttsClient, sendControl, sendAudio }) {
  const state = createSessionState();
  let history = [];
  let activeLlmHandle = null;

  sttClient.on('speechStarted', () => {
    if (state.current === 'thinking' || state.current === 'speaking') {
      if (activeLlmHandle) activeLlmHandle.abort();
      ttsClient.abort();
      state.transition('USER_INTERRUPTED');
      sendControl({ type: 'interrupt' });
    }
  });

  sttClient.on('transcriptFinal', (transcript) => {
    state.transition('TRANSCRIPT_FINAL');
    sendControl({ type: 'transcript', text: transcript });
    runTurn(transcript);
  });

  ttsClient.on('audioChunk', (buffer) => {
    sendAudio(buffer);
  });

  ttsClient.on('turnComplete', () => {
    if (state.current === 'speaking') {
      state.transition('TTS_ENDED');
      sendControl({ type: 'state', state: 'listening' });
    }
  });

  function runTurn(userText) {
    const turnStartedAt = Date.now();
    const messages = buildMessages({ systemPrompt, history, newUserText: userText });
    let assistantText = '';
    let ttsStarted = false;
    let firstTokenLogged = false;

    ttsClient.connect().then(() => {
      const chunker = createTextChunker({
        onFlush: (text) => {
          if (!ttsStarted) {
            ttsStarted = true;
            state.transition('TTS_STARTED');
            sendControl({ type: 'state', state: 'speaking' });
            console.log('[latency] first_tts_chunk_ms', Date.now() - turnStartedAt);
          }
          ttsClient.sendText(text);
        },
      });

      activeLlmHandle = llmClient.streamCompletion(messages, {
        onToken: (token) => {
          if (!firstTokenLogged) {
            firstTokenLogged = true;
            console.log('[latency] first_llm_token_ms', Date.now() - turnStartedAt);
          }
          assistantText += token;
          chunker.push(token);
        },
        onDone: () => {
          chunker.end();
          ttsClient.endTurn();
          history = [
            ...history,
            { role: 'user', content: userText },
            { role: 'assistant', content: assistantText },
          ];
        },
        onError: (err) => {
          chunker.end();
          ttsClient.endTurn();
          sendControl({ type: 'error', message: err.message });
        },
      });
    });
  }

  function start() {
    state.transition('CALL_STARTED');
    return sttClient.connect();
  }

  function stop() {
    state.transition('CALL_ENDED');
    sttClient.close();
    ttsClient.abort();
  }

  function handleAudioChunk(buffer) {
    sttClient.sendAudioChunk(buffer);
  }

  return { start, stop, handleAudioChunk };
}

module.exports = { createSessionOrchestrator };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `node --test src/sessionOrchestrator.test.js`
Expected: PASS, 2 tests.

- [ ] **Step 5: Run the full test suite**

Run: `npm test`
Expected: all test files across Tasks 2-9 pass (23 tests total).

- [ ] **Step 6: Commit**

```bash
git add src/sessionOrchestrator.js src/sessionOrchestrator.test.js
git commit -m "feat: add session orchestrator wiring STT, LLM, and TTS with barge-in"
```

---

### Task 10: HTTP + WebSocket server (real wiring)

Wires the real `openai` SDK client and real `ws` connections into the orchestrator
built in Task 9, and serves the static frontend. This task touches live external
services when a browser actually connects, so per the spec's testing section it is
verified manually, not with an automated test that would need real API keys in CI.

**Files:**
- Create: `src/server.js`

**Interfaces:**
- Consumes: `loadConfig` (Task 2), `createRealtimeSttClient` (Task 6), `createLlmClient` (Task 7), `createTtsClient` (Task 8), `createSessionOrchestrator` (Task 9)
- Produces: a running HTTP server (static file serving for `public/`) with a WebSocket upgrade at `/ws` — one `sessionOrchestrator` per WebSocket connection.

- [ ] **Step 1: Write the server**

```javascript
// src/server.js
require('dotenv').config();

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { WebSocketServer } = require('ws');
const OpenAI = require('openai');

const { loadConfig } = require('./config');
const { createRealtimeSttClient } = require('./realtimeSttClient');
const { createLlmClient } = require('./llmClient');
const { createTtsClient } = require('./ttsClient');
const { createSessionOrchestrator } = require('./sessionOrchestrator');

const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
};

function serveStatic(req, res) {
  const publicDir = path.join(__dirname, '..', 'public');
  const requestedPath = req.url === '/' ? '/index.html' : req.url;
  const filePath = path.join(publicDir, requestedPath);

  if (!filePath.startsWith(publicDir)) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }

  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404);
      res.end('Not found');
      return;
    }
    const ext = path.extname(filePath);
    res.writeHead(200, { 'Content-Type': CONTENT_TYPES[ext] || 'application/octet-stream' });
    res.end(data);
  });
}

function main() {
  const config = loadConfig();
  const openaiClient = new OpenAI({ apiKey: config.openaiApiKey });

  const server = http.createServer(serveStatic);
  const wss = new WebSocketServer({ server, path: '/ws' });

  wss.on('connection', (browserWs) => {
    const sttClient = createRealtimeSttClient({
      apiKey: config.openaiApiKey,
      model: config.openaiRealtimeModel,
      transcribeModel: config.openaiTranscribeModel,
    });
    const llmClient = createLlmClient({ openaiClient });
    const ttsClient = createTtsClient({
      apiKey: config.elevenLabsApiKey,
      voiceId: config.elevenLabsVoiceId,
      modelId: config.elevenLabsModelId,
    });

    const orchestrator = createSessionOrchestrator({
      systemPrompt: config.systemPrompt,
      sttClient,
      llmClient,
      ttsClient,
      sendControl: (msg) => {
        if (browserWs.readyState === browserWs.OPEN) browserWs.send(JSON.stringify(msg));
      },
      sendAudio: (buffer) => {
        if (browserWs.readyState === browserWs.OPEN) browserWs.send(buffer, { binary: true });
      },
    });

    orchestrator.start().catch((err) => {
      console.error('[server] failed to start session', err);
      browserWs.send(JSON.stringify({ type: 'error', message: 'Không thể kết nối tới dịch vụ giọng nói.' }));
      browserWs.close();
    });

    browserWs.on('message', (data, isBinary) => {
      if (isBinary) {
        orchestrator.handleAudioChunk(data);
      }
    });

    browserWs.on('close', () => {
      orchestrator.stop();
    });
  });

  server.listen(config.port, () => {
    console.log(`[server] listening on http://localhost:${config.port}`);
  });
}

main();

module.exports = { serveStatic };
```

- [ ] **Step 2: Manual smoke test — static file serving**

Create a placeholder `public/index.html` containing just `<!doctype html><title>Voice Agent</title>` (Task 11 replaces it with the real page).

Run: `OPENAI_API_KEY=x ELEVENLABS_API_KEY=y ELEVENLABS_VOICE_ID=z npm start`
Then: `curl -i http://localhost:3000/`
Expected: `200 OK` with the placeholder HTML body.

Stop the server (Ctrl+C).

- [ ] **Step 3: Commit**

```bash
git add src/server.js public/index.html
git commit -m "feat: add HTTP/WebSocket server wiring real clients into the orchestrator"
```

---

### Task 11: Frontend mic capture (AudioWorklet + WebSocket)

**Files:**
- Create: `public/pcm-encoder-processor.js`
- Modify: `public/client.js` (create if not present from Task 10's placeholder step)

**Interfaces:**
- Produces: an `AudioWorkletProcessor` registered as `'pcm-encoder-processor'` that posts `Int16Array` PCM buffers to the main thread; a `client.js` that captures mic audio at 24kHz and sends each buffer as a binary WebSocket frame to `/ws`.

- [ ] **Step 1: Create the AudioWorklet processor**

```javascript
// public/pcm-encoder-processor.js
class PcmEncoderProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0] && input[0].length > 0) {
      const floatSamples = input[0];
      const int16Samples = new Int16Array(floatSamples.length);
      for (let i = 0; i < floatSamples.length; i += 1) {
        const clamped = Math.max(-1, Math.min(1, floatSamples[i]));
        int16Samples[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      }
      this.port.postMessage(int16Samples.buffer, [int16Samples.buffer]);
    }
    return true;
  }
}

registerProcessor('pcm-encoder-processor', PcmEncoderProcessor);
```

- [ ] **Step 2: Write `client.js` mic capture + WebSocket send**

```javascript
// public/client.js
let audioContext;
let micStream;
let socket;

async function startCall() {
  audioContext = new AudioContext({ sampleRate: 24000 });
  await audioContext.audioWorklet.addModule('pcm-encoder-processor.js');

  micStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
  const source = audioContext.createMediaStreamSource(micStream);
  const encoderNode = new AudioWorkletNode(audioContext, 'pcm-encoder-processor');
  source.connect(encoderNode);

  socket = new WebSocket(`ws://${location.host}/ws`);
  socket.binaryType = 'arraybuffer';

  encoderNode.port.onmessage = (event) => {
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(event.data);
    }
  };

  socket.addEventListener('open', () => setStatus('listening'));
  socket.addEventListener('close', () => setStatus('idle'));
  socket.addEventListener('message', handleServerMessage);
}

function stopCall() {
  if (socket) socket.close();
  if (micStream) micStream.getTracks().forEach((track) => track.stop());
  if (audioContext) audioContext.close();
  setStatus('idle');
}

function setStatus(status) {
  const el = document.getElementById('status');
  if (el) el.textContent = status;
}

function handleServerMessage(event) {
  // Playback handling added in Task 12.
  if (typeof event.data === 'string') {
    const msg = JSON.parse(event.data);
    if (msg.type === 'transcript') appendTranscript('user', msg.text);
    if (msg.type === 'error') appendTranscript('error', msg.message);
  }
}

function appendTranscript(role, text) {
  const el = document.getElementById('transcript');
  if (!el) return;
  const line = document.createElement('div');
  line.textContent = `[${role}] ${text}`;
  el.appendChild(line);
}

document.getElementById('startButton').addEventListener('click', startCall);
document.getElementById('endButton').addEventListener('click', stopCall);
```

- [ ] **Step 3: Manual test — mic capture reaches the backend**

Update `public/index.html` per Task 12 (needs the `startButton`/`endButton`/`status` elements) — if Task 12 hasn't run yet, add a temporary minimal markup with those three IDs to test this task in isolation.

Run: `npm start` with real `OPENAI_API_KEY`/`ELEVENLABS_API_KEY`/`ELEVENLABS_VOICE_ID` in `.env`.
Open `http://localhost:3000` in Chrome, open DevTools → Network → WS, click Start.
Expected: WebSocket connects, binary frames appear flowing browser → server as you speak into the mic; server console shows no `[server] failed to start session` error (confirms the STT client connected).

- [ ] **Step 4: Commit**

```bash
git add public/pcm-encoder-processor.js public/client.js
git commit -m "feat: add mic capture via AudioWorklet and WebSocket streaming"
```

---

### Task 12: Frontend playback (MediaSource) + barge-in + UI

**Files:**
- Create: `public/index.html`
- Modify: `public/client.js`

**Interfaces:**
- Produces: gapless MP3 chunk playback via `MediaSource`/`SourceBuffer`; on receiving `{type:'interrupt'}` the current `<audio>`/`MediaSource` is torn down and playback stops immediately; Start/End call buttons and live status/transcript.

- [ ] **Step 1: Write `public/index.html`**

```html
<!doctype html>
<html lang="vi">
  <head>
    <meta charset="utf-8" />
    <title>Voice Agent</title>
    <style>
      body { font-family: sans-serif; max-width: 480px; margin: 40px auto; }
      #status { font-weight: bold; }
      #transcript { margin-top: 16px; border-top: 1px solid #ccc; padding-top: 8px; }
      button { font-size: 16px; padding: 8px 16px; margin-right: 8px; }
    </style>
  </head>
  <body>
    <h1>Voice Agent</h1>
    <p>Trạng thái: <span id="status">idle</span></p>
    <button id="startButton">Bắt đầu gọi</button>
    <button id="endButton">Kết thúc</button>
    <div id="transcript"></div>
    <script src="client.js"></script>
  </body>
</html>
```

- [ ] **Step 2: Add MediaSource playback and barge-in handling to `client.js`**

```javascript
// public/client.js (additions — replace handleServerMessage and add playback helpers)
let mediaSource;
let sourceBuffer;
let audioEl;
let pendingChunks = [];

function startPlaybackStream() {
  audioEl = new Audio();
  mediaSource = new MediaSource();
  audioEl.src = URL.createObjectURL(mediaSource);
  pendingChunks = [];

  mediaSource.addEventListener('sourceopen', () => {
    sourceBuffer = mediaSource.addSourceBuffer('audio/mpeg');
    sourceBuffer.addEventListener('updateend', appendNextPendingChunk);
  });

  audioEl.play().catch(() => {
    /* playback resumes once the user has interacted with the page */
  });
}

function appendAudioChunk(arrayBuffer) {
  if (!mediaSource) startPlaybackStream();
  pendingChunks.push(arrayBuffer);
  appendNextPendingChunk();
}

function appendNextPendingChunk() {
  if (!sourceBuffer || sourceBuffer.updating || pendingChunks.length === 0) return;
  sourceBuffer.appendBuffer(pendingChunks.shift());
}

function stopPlaybackStream() {
  if (audioEl) {
    audioEl.pause();
    audioEl.removeAttribute('src');
  }
  mediaSource = null;
  sourceBuffer = null;
  pendingChunks = [];
  audioEl = null;
}

function handleServerMessage(event) {
  if (typeof event.data === 'string') {
    const msg = JSON.parse(event.data);
    if (msg.type === 'transcript') appendTranscript('user', msg.text);
    if (msg.type === 'state') setStatus(msg.state);
    if (msg.type === 'error') appendTranscript('error', msg.message);
    if (msg.type === 'interrupt') stopPlaybackStream();
    return;
  }
  appendAudioChunk(event.data);
}
```

- [ ] **Step 3: Manual end-to-end test**

Run: `npm start` with real API keys in `.env`.
Open `http://localhost:3000`, click "Bắt đầu gọi", speak a short question in Vietnamese into the mic.
Expected: status goes `listening` → `speaking`, transcript shows the recognized text, agent's Vietnamese voice plays back through the browser speakers.

Manual barge-in test: while the agent is speaking, speak again.
Expected: playback stops immediately (within roughly one audio chunk), status returns to `listening`, and a new turn starts from your new speech.

- [ ] **Step 4: Commit**

```bash
git add public/index.html public/client.js
git commit -m "feat: add streaming audio playback, barge-in flush, and call UI"
```

---

### Task 13: README and manual verification checklist

**Files:**
- Create: `README.md`

**Interfaces:**
- Produces: run instructions and the manual test checklist from the spec's Testing section, so latency and barge-in behavior can be verified against the real APIs.

- [ ] **Step 1: Write `README.md`**

```markdown
# Voice Agent (Vietnamese)

Low-latency Vietnamese voice agent. Browser mic -> Node.js backend -> OpenAI
Realtime API (speech-to-text) -> GPT-4.1-mini (streaming) -> ElevenLabs
(streaming TTS) -> browser playback. See
`docs/superpowers/specs/2026-08-03-voice-agent-design.md` for the full design.

## Setup

1. `npm install`
2. Copy `.env.example` to `.env` and fill in `OPENAI_API_KEY`,
   `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID`.
3. Edit `config/systemDoc.md` with the real reference document for the agent
   (it is embedded directly into the system prompt).
4. `npm start`, then open `http://localhost:3000` in Chrome.

## Automated tests

`npm test` runs the unit tests (pure logic + client wrappers against fakes —
no real API calls, no keys required).

## Manual verification checklist

Run these against the real APIs (requires valid keys in `.env`):

- [ ] Click "Bắt đầu gọi", speak a short question — transcript appears, agent
      responds with Vietnamese audio.
- [ ] Ask a question answerable from `config/systemDoc.md` — the response
      reflects the document content.
- [ ] Ask something outside the document — the agent says it doesn't know
      rather than inventing an answer.
- [ ] While the agent is speaking, speak again (barge-in) — playback stops
      within roughly one audio chunk and a new turn starts.
- [ ] Click "Kết thúc" mid-call — the WebSocket, Realtime session, and any
      open ElevenLabs stream all close (check server logs for no dangling
      connections/errors).
- [ ] Check the server console `[latency]` log lines for
      `first_llm_token_ms` and `first_tts_chunk_ms` across a few turns to
      spot regressions or outliers.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add setup instructions and manual verification checklist"
```

---

## Self-Review Notes

- **Spec coverage:** architecture (Task 9/10), frontend capture/playback (Tasks 11-12), barge-in (Tasks 5/9/12), document placeholder (Task 1/13), error handling for each external service (Task 9 `onError` + Task 10 `orchestrator.start().catch`), latency logging (Task 9 `console.log('[latency] ...')`), manual testing checklist (Task 13) — all spec sections have a task.
- **Placeholder scan:** no TBD/TODO; `config/systemDoc.md` is an intentional placeholder per the spec, filled with real instructions on how to replace it (Task 13 README step).
- **Type/signature consistency checked:** `sttClient`/`ttsClient` event names and method signatures match between their defining tasks (6, 8) and consuming task (9); `llmClient.streamCompletion` signature matches between Task 7 and Task 9; `sessionState` event names match between Task 5 and Task 9's usage.
- **Scope:** single cohesive pipeline, matches the approved spec; multi-call scaling and push-to-talk explicitly out of scope per the spec and not included here.
