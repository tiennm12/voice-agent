require('dotenv').config();
const WebSocket = require('ws');

const voiceId = process.env.ELEVENLABS_VOICE_ID;
const modelId = process.env.ELEVENLABS_MODEL_ID;
const apiKey = process.env.ELEVENLABS_API_KEY;

console.log('voiceId set:', !!voiceId, 'len', voiceId ? voiceId.length : 0);
console.log('modelId:', modelId);
console.log('apiKey set:', !!apiKey, 'len', apiKey ? apiKey.length : 0);

const url = `wss://api.elevenlabs.io/v1/text-to-speech/${voiceId}/stream-input?model_id=${modelId}&output_format=mp3_44100_128`;
console.log('connecting to', url);

const ws = new WebSocket(url, { headers: { 'xi-api-key': apiKey } });

ws.on('open', () => {
  console.log('OPEN');
  ws.send(JSON.stringify({ text: ' ', voice_settings: { stability: 0.5, similarity_boost: 0.75 } }));
});
ws.on('message', (d) => console.log('MSG', d.toString().slice(0, 500)));
ws.on('error', (e) => console.log('ERR', e.message));
ws.on('close', (code, reason) => {
  console.log('CLOSE', code, reason.toString());
  process.exit(0);
});
setTimeout(() => {
  console.log('TIMEOUT - no close after 10s');
  process.exit(1);
}, 10000);
