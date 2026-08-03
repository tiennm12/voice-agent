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
