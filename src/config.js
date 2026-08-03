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
