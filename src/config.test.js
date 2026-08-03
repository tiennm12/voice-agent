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
