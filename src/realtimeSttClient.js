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
    if (!ws || ws.readyState !== ws.OPEN) return;
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
