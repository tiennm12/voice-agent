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
