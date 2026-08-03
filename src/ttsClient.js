const { EventEmitter } = require('node:events');

function defaultCreateWebSocket(url, options) {
  const WebSocket = require('ws');
  return new WebSocket(url, options);
}

function createTtsClient({ apiKey, voiceId, modelId, createWebSocket = defaultCreateWebSocket }) {
  const emitter = new EventEmitter();
  let ws = null;
  // Set right before we close the socket ourselves (abort()), or once the server
  // has told us the turn is finished (isFinal) — ElevenLabs closes the socket on
  // its own right after that, which is expected and not a "disconnected" event.
  let expectClose = false;

  function connect() {
    expectClose = false;
    return new Promise((resolve, reject) => {
      const url = `wss://api.elevenlabs.io/v1/text-to-speech/${voiceId}/stream-input?model_id=${modelId}&output_format=mp3_44100_128`;
      // Capture this connection's socket locally. `ws` (the shared/outward-facing
      // reference) gets reassigned on every connect() call (once per turn), so a
      // handler that closed over `ws` instead of this local `socket` could fire for
      // an orphaned previous-turn socket after a new turn's connect() has already
      // reassigned `ws` — see the 'close' handler below.
      const socket = createWebSocket(url, { headers: { 'xi-api-key': apiKey } });
      ws = socket;

      socket.on('open', () => {
        socket.send(JSON.stringify({
          text: ' ',
          voice_settings: { stability: 0.5, similarity_boost: 0.75 },
        }));
        resolve();
      });

      socket.on('message', (data) => {
        if (socket !== ws) {
          // Stale message from a socket a later connect() already superseded —
          // see the 'close' handler below for why this must not touch shared
          // state (expectClose) or emit events attributed to the current turn.
          return;
        }
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
          expectClose = true;
          emitter.emit('turnComplete');
        }
      });

      socket.on('error', (err) => {
        if (socket !== ws) {
          return;
        }
        emitter.emit('error', err);
        reject(err);
      });

      socket.on('close', () => {
        if (socket !== ws) {
          // This socket has already been superseded by a later connect() call (e.g.
          // the next turn started before this one's server-initiated close arrived).
          // It's neither an "expected" nor "unexpected" close for the *current*
          // connection — the shared `expectClose` flag belongs to `ws`, not to this
          // stale socket, so touching it here would either false-alarm on the live
          // connection or wrongly mark a future real drop as already-expected.
          return;
        }
        // ws.send() on a closed socket is a silent no-op, so without this a mid-turn
        // drop goes completely unnoticed. Only surface it when neither we nor the
        // server already wound the turn down normally.
        if (!expectClose) {
          expectClose = true;
          emitter.emit('disconnected');
        }
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
    expectClose = true;
    if (ws) {
      ws.close();
      ws = null;
    }
  }

  return { connect, sendText, endTurn, abort, on: emitter.on.bind(emitter) };
}

module.exports = { createTtsClient };
