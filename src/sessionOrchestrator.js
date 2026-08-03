// src/sessionOrchestrator.js
const { createSessionState } = require('./sessionState');
const { createTextChunker } = require('./textChunker');
const { buildMessages } = require('./promptBuilder');

const LLM_ERROR_APOLOGY = 'Xin lỗi, tôi gặp sự cố kỹ thuật, bạn nói lại được không?';

function createSessionOrchestrator({
  systemPrompt,
  sttClient,
  llmClient,
  ttsClient,
  sendControl,
  sendAudio,
  sessionId = '',
}) {
  const logPrefix = `[${sessionId}]`;
  const state = createSessionState();
  let history = [];
  let activeLlmHandle = null;
  let turnCounter = 0;
  // Identifies the turn (if any) that is still allowed to proceed past the
  // ttsClient.connect() gap. A barge-in invalidates it by resetting this to 0,
  // which no real turn id (always >= 1) can ever match again.
  let currentTurnId = 0;

  function abandonCurrentTurn() {
    currentTurnId = 0;
    if (activeLlmHandle) {
      activeLlmHandle.abort();
      activeLlmHandle = null;
    }
  }

  sttClient.on('speechStarted', () => {
    if (state.current === 'thinking' || state.current === 'speaking') {
      abandonCurrentTurn(); // supersede any in-flight turn (including ones still awaiting TTS connect)
      ttsClient.abort();
      state.transition('USER_INTERRUPTED');
      sendControl({ type: 'interrupt' });
    }
  });

  sttClient.on('transcriptFinal', (transcript) => {
    if (state.current !== 'listening') {
      // Duplicate/out-of-order STT event (e.g. arrives while already thinking/speaking).
      // TRANSCRIPT_FINAL is only a valid transition from 'listening'; drop it instead of
      // letting state.transition throw an uncaught exception.
      return;
    }
    state.transition('TRANSCRIPT_FINAL');
    sendControl({ type: 'transcript', text: transcript });
    runTurn(transcript);
  });

  sttClient.on('error', (err) => {
    sendControl({ type: 'error', message: err.message });
  });

  sttClient.on('disconnected', () => {
    // Upstream OpenAI Realtime WS dropped unexpectedly (network blip, session duration
    // cap, etc). A full retry-then-end-call flow is out of scope for now — the minimum
    // bar is: log it, tell the browser, and don't leave the session in a state that
    // looks alive but can never make progress again. CALL_ENDED is special-cased in
    // sessionState to always succeed and land on 'idle', so this never throws.
    //
    // Abandon any in-flight turn first: without this, a still-running turn keeps
    // passing isCurrentTurn() after we've already declared the call over, so a
    // queued chunk flushing later would try state.transition('TTS_STARTED') from
    // 'idle' (throws — the exact raw-error-leak fix #2 was meant to stop, re-entering
    // through here), or the agent could end up speaking on a call already ended.
    console.error(`${logPrefix} sttClient disconnected unexpectedly`);
    abandonCurrentTurn();
    ttsClient.abort();
    sendControl({ type: 'error', message: 'Mất kết nối dịch vụ nhận diện giọng nói.' });
    if (state.current !== 'idle') {
      state.transition('CALL_ENDED');
    }
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

  ttsClient.on('error', (err) => {
    sendControl({ type: 'error', message: err.message });
  });

  ttsClient.on('disconnected', () => {
    // Upstream ElevenLabs WS dropped unexpectedly mid-turn — this can happen either
    // after the first chunk went out (state 'speaking') or after connect() resolved
    // but before anything was flushed yet (state still 'thinking'). Recovering only
    // from 'speaking' left the 'thinking' case wedged forever: the turn would carry
    // on, write into the dead socket (silent no-op), turnComplete would never arrive,
    // and every subsequent transcriptFinal would be dropped by the
    // state.current !== 'listening' guard with no way back short of a reload.
    console.error(`${logPrefix} ttsClient disconnected unexpectedly`);
    abandonCurrentTurn();
    sendControl({ type: 'error', message: 'Mất kết nối dịch vụ giọng nói.' });
    if (state.current === 'speaking' || state.current === 'thinking') {
      state.transition('USER_INTERRUPTED');
      sendControl({ type: 'state', state: 'listening' });
    }
  });

  function runTurn(userText) {
    const turnStartedAt = Date.now();
    const messages = buildMessages({ systemPrompt, history, newUserText: userText });
    let assistantText = '';
    let ttsStarted = false;
    let firstTokenLogged = false;
    let firstTtsChunkLogged = false;
    let ttsReady = false;
    let llmDonePending = false; // endTurn() is waiting on ttsReady
    const pendingChunks = [];

    const myTurnId = ++turnCounter;
    currentTurnId = myTurnId;
    const isCurrentTurn = () => myTurnId === currentTurnId;

    // Sends one chunk of text to ElevenLabs, handling the thinking->speaking state
    // transition and the first-chunk latency log on whichever chunk is actually the
    // first one to reach the wire (a chunk sent immediately, or the first one flushed
    // out of the queue once the TTS connection comes up).
    function sendChunkToTts(text) {
      if (!ttsStarted) {
        ttsStarted = true;
        state.transition('TTS_STARTED');
        sendControl({ type: 'state', state: 'speaking' });
      }
      if (!firstTtsChunkLogged) {
        firstTtsChunkLogged = true;
        console.log(`${logPrefix} [latency] first_tts_chunk_ms`, Date.now() - turnStartedAt);
      }
      ttsClient.sendText(text);
    }

    function flushPendingChunks() {
      const queued = pendingChunks.splice(0, pendingChunks.length);
      queued.forEach(sendChunkToTts);
      if (llmDonePending) {
        llmDonePending = false;
        ttsClient.endTurn();
      }
    }

    // Kick off the TTS handshake and the LLM stream in parallel — nothing about
    // starting the LLM call depends on TTS being ready, and serializing them just
    // adds a WebSocket handshake to time-to-first-token on every turn.
    ttsClient
      .connect()
      .then(() => {
        if (!isCurrentTurn()) {
          // A barge-in superseded this turn while TTS was still connecting. The
          // barge-in handler already moved the session back to 'listening' and
          // aborted what needed aborting — nothing left to do here.
          return;
        }
        ttsReady = true;
        flushPendingChunks();
      })
      .catch((err) => {
        if (!isCurrentTurn()) {
          // Already superseded by a barge-in; that handler already restored 'listening'.
          return;
        }
        // TTS is unreachable for this turn — give up on it so a late-arriving LLM
        // token/onDone can't try to use a connection that will never come up.
        abandonCurrentTurn();
        sendControl({ type: 'error', message: err.message });
        if (state.current === 'thinking') {
          // Recover instead of leaving the session stuck in 'thinking' forever — TTS_ENDED
          // is only valid from 'speaking', so USER_INTERRUPTED is the transition available
          // from 'thinking' back to 'listening'.
          state.transition('USER_INTERRUPTED');
          sendControl({ type: 'state', state: 'listening' });
        }
      });

    const chunker = createTextChunker({
      onFlush: (text) => {
        if (!isCurrentTurn()) return; // superseded by a barge-in; drop it
        if (ttsReady) {
          sendChunkToTts(text);
        } else {
          // TTS handshake still in flight — queue it and send in order once connected.
          pendingChunks.push(text);
        }
      },
    });

    activeLlmHandle = llmClient.streamCompletion(messages, {
      onToken: (token) => {
        if (!isCurrentTurn()) return; // superseded by a barge-in; drop it
        if (!firstTokenLogged) {
          firstTokenLogged = true;
          console.log(`${logPrefix} [latency] first_llm_token_ms`, Date.now() - turnStartedAt);
        }
        assistantText += token;
        chunker.push(token);
      },
      onDone: () => {
        if (!isCurrentTurn()) return; // superseded by a barge-in; drop it
        chunker.end();
        if (ttsReady) {
          ttsClient.endTurn();
        } else {
          llmDonePending = true;
        }
        history = [
          ...history,
          { role: 'user', content: userText },
          { role: 'assistant', content: assistantText },
        ];
      },
      onError: (err) => {
        if (!isCurrentTurn()) return; // superseded by a barge-in; drop it
        console.error(`${logPrefix} llm error`, err);
        if (ttsReady) {
          // Spoken fallback per spec: apologize in Vietnamese and keep the session
          // alive for the next turn instead of surfacing the raw error message.
          sendChunkToTts(LLM_ERROR_APOLOGY);
          ttsClient.endTurn();
        } else {
          // TTS itself isn't available for this turn (still connecting, or never
          // will connect) — nothing can speak the apology, so fall back to the
          // error-control-message path and recover the state machine directly.
          abandonCurrentTurn();
          ttsClient.abort();
          sendControl({ type: 'error', message: err.message });
          if (state.current === 'thinking') {
            state.transition('USER_INTERRUPTED');
            sendControl({ type: 'state', state: 'listening' });
          }
        }
      },
    });
  }

  function start() {
    state.transition('CALL_STARTED');
    return sttClient.connect();
  }

  function stop() {
    // Invalidate the turn fence and abort any in-flight LLM request first, so a
    // pending ttsClient.connect().then() (still waiting when the browser hangs up)
    // can't pass its turn-id check and start a brand-new LLM request after the call
    // already ended, and so we don't keep paying for tokens nobody will hear.
    abandonCurrentTurn();
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
