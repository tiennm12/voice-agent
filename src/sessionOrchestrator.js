// src/sessionOrchestrator.js
const { createSessionState } = require('./sessionState');
const { createTextChunker } = require('./textChunker');
const { buildMessages } = require('./promptBuilder');

function createSessionOrchestrator({ systemPrompt, sttClient, llmClient, ttsClient, sendControl, sendAudio }) {
  const state = createSessionState();
  let history = [];
  let activeLlmHandle = null;
  let turnCounter = 0;
  // Identifies the turn (if any) that is still allowed to proceed past the
  // ttsClient.connect() gap. A barge-in invalidates it by resetting this to 0,
  // which no real turn id (always >= 1) can ever match again.
  let currentTurnId = 0;

  sttClient.on('speechStarted', () => {
    if (state.current === 'thinking' || state.current === 'speaking') {
      currentTurnId = 0; // supersede any in-flight turn (including ones still awaiting TTS connect)
      if (activeLlmHandle) activeLlmHandle.abort();
      activeLlmHandle = null;
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

  function runTurn(userText) {
    const turnStartedAt = Date.now();
    const messages = buildMessages({ systemPrompt, history, newUserText: userText });
    let assistantText = '';
    let ttsStarted = false;
    let firstTokenLogged = false;
    const myTurnId = ++turnCounter;
    currentTurnId = myTurnId;

    ttsClient.connect().then(() => {
      if (myTurnId !== currentTurnId) {
        // A barge-in superseded this turn while TTS was still connecting. Don't start
        // the LLM call, and don't touch activeLlmHandle/state — the barge-in handler
        // already moved the session back to 'listening'.
        return;
      }

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
    }).catch((err) => {
      if (myTurnId !== currentTurnId) {
        // Already superseded by a barge-in; that handler already restored 'listening'.
        return;
      }
      sendControl({ type: 'error', message: err.message });
      if (state.current === 'thinking') {
        // Recover instead of leaving the session stuck in 'thinking' forever — TTS_ENDED
        // is only valid from 'speaking', so USER_INTERRUPTED is the transition available
        // from 'thinking' back to 'listening'.
        state.transition('USER_INTERRUPTED');
        sendControl({ type: 'state', state: 'listening' });
      }
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
