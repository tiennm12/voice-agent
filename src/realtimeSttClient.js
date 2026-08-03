const { EventEmitter } = require('node:events');

function defaultCreateWebSocket(url, options) {
  const WebSocket = require('ws');
  return new WebSocket(url, options);
}

function createRealtimeSttClient({ apiKey, model, transcribeModel, createWebSocket = defaultCreateWebSocket }) {
  const emitter = new EventEmitter();
  let ws = null;
  // Set right before we close the socket ourselves (close()) so the 'close' handler
  // below can tell a deliberate shutdown apart from an unexpected drop (network
  // blip, session duration cap, server-side error) and only emit 'disconnected'
  // for the latter.
  let expectClose = false;

  function connect() {
    expectClose = false;
    return new Promise((resolve, reject) => {
      // Capture this connection's socket locally. `ws` (the shared/outward-facing
      // reference) would get reassigned by a subsequent connect() call, so a handler
      // that closed over `ws` instead of this local `socket` could fire for an
      // orphaned previous-connection socket after a newer connect() has already
      // reassigned `ws` — see the 'close' handler below.
      const socket = createWebSocket(`wss://api.openai.com/v1/realtime?model=${model}`, {
        headers: { Authorization: `Bearer ${apiKey}` },
      });
      ws = socket;

      socket.on('open', () => {
        socket.send(JSON.stringify({
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

      socket.on('message', (data) => {
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

      socket.on('error', (err) => {
        emitter.emit('error', err);
        reject(err);
      });

      socket.on('close', () => {
        if (socket !== ws) {
          // This socket has already been superseded by a later connect() call.
          // The shared `expectClose` flag belongs to `ws`, not to this stale
          // socket, so touching it here would either false-alarm on the live
          // connection or wrongly mark a future real drop as already-expected.
          return;
        }
        // ws.send() on a closed socket is a silent no-op, so without this the whole
        // pipeline goes dead with zero signal. Only surface it when we didn't ask
        // for the close ourselves.
        if (!expectClose) {
          expectClose = true;
          emitter.emit('disconnected');
        }
      });
    });
  }

  function sendAudioChunk(buffer) {
    if (!ws || ws.readyState !== ws.OPEN) return;
    ws.send(JSON.stringify({ type: 'input_audio_buffer.append', audio: buffer.toString('base64') }));
  }

  function close() {
    expectClose = true;
    if (ws) {
      ws.close();
      ws = null;
    }
  }

  return { connect, sendAudioChunk, close, on: emitter.on.bind(emitter) };
}

module.exports = { createRealtimeSttClient };
