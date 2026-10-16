let audioContext;
let micStream;
let socket;
let mediaSource;
let sourceBuffer;
let audioEl;
let pendingChunks = [];

// Plays the agent's MP3 stream through an <audio> element (see startPlaybackStream).
const mp3Player = { play: appendAudioChunk, stop: stopPlaybackStream };
// What plays the agent's voice in the current call: { play(arrayBuffer), stop() }.
let player = mp3Player;
let onCallEnded = null;

// With no options this is a browser-mic call. The bridge page (bridge.js) passes:
//   inputStream  the caller's audio, used instead of the microphone
//   meta         { caller, pbxSessionId } reported to the server for the call record
//   audio        format to ask the server for ('pcm'), paired with a player that takes it
//   player       { play(arrayBuffer), stop() } replacing the built-in MP3 player
//   onEnded      called once the call has ended, whoever ended it
async function startCall({ inputStream, meta, audio, player: callPlayer, onEnded } = {}) {
  try {
    player.stop(); // drop whatever a previous call left queued
    player = callPlayer || mp3Player;
    onCallEnded = onEnded || null;
    audioContext = new AudioContext({ sampleRate: 24000 });
    // The realtime pipeline speaks raw PCM, which the MP3 player can't play.
    if (!callPlayer && window.callOptions?.pipeline === 'realtime') {
      audio = 'pcm';
      player = createCallPlayer(audioContext, audioContext.destination);
    }
    await audioContext.audioWorklet.addModule('pcm-encoder-processor.js');

    // /call.html?raw=1 turns Chrome's own echo/noise/gain processing off, to compare on a bad mic.
    const processing = !new URLSearchParams(location.search).has('raw');
    micStream = inputStream || await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: processing,
        noiseSuppression: processing,
        autoGainControl: processing,
      },
    });
    if (!inputStream) {
      const [track] = micStream.getAudioTracks();
      appendTranscript('mic', `${track.label} (${processing ? 'xử lý của Chrome' : 'thô, không xử lý'})`);
    }
    const source = audioContext.createMediaStreamSource(micStream);
    const encoderNode = new AudioWorkletNode(audioContext, 'pcm-encoder-processor');
    source.connect(encoderNode);

    // AudioWorkletNode process() callbacks are only reliably guaranteed to fire once
    // the node is connected all the way to destination. Route through a silent gain
    // node so the graph is fully connected without producing audible output.
    const silentGain = audioContext.createGain();
    silentGain.gain.value = 0;
    encoderNode.connect(silentGain).connect(audioContext.destination);

    const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    // The test page (call.html) sets window.callOptions: pipeline settings for this call only.
    const query = new URLSearchParams(window.callOptions || {});
    if (audio) query.set('audio', audio);
    const callSocket = new WebSocket(`${wsProtocol}//${location.host}${location.pathname.replace(/[^/]*$/, "")}ws${query.size ? `?${query}` : ''}`);
    callSocket.binaryType = 'arraybuffer';
    socket = callSocket;
    // A socket the page has moved on from (call ended, maybe a new one started) can
    // still deliver events for a moment: they must not touch the current call.
    const isCurrent = () => socket === callSocket;

    encoderNode.port.onmessage = (event) => {
      if (callSocket.readyState === WebSocket.OPEN) {
        callSocket.send(event.data);
      }
    };

    callSocket.addEventListener('open', () => {
      setStatus('listening');
      if (meta) callSocket.send(JSON.stringify({ type: 'callMeta', ...meta }));
    });
    callSocket.addEventListener('close', () => {
      if (isCurrent()) releaseCall();
    });
    callSocket.addEventListener('error', () => {
      if (!isCurrent()) return;
      setStatus('error');
      appendTranscript('error', 'Không thể kết nối.');
    });
    callSocket.addEventListener('message', (event) => {
      if (isCurrent()) handleServerMessage(event);
    });
  } catch (err) {
    releaseCall(); // also tells the embedding page (onEnded) that this call is not happening
    setStatus('error');
    const micPermissionErrors = ['NotAllowedError', 'PermissionDeniedError', 'NotFoundError', 'SecurityError'];
    if (err && micPermissionErrors.includes(err.name)) {
      appendTranscript('error', 'Không thể truy cập microphone. Kiểm tra quyền truy cập.');
    } else {
      appendTranscript('error', 'Không thể kết nối.');
    }
  }
}

// The user ends the call: cut the agent off mid-sentence too.
function stopCall() {
  player.stop();
  releaseCall();
}

// Ends the call from our side, or tidies up after the agent hung up. Audio already
// received keeps playing, so a goodbye isn't clipped by the hangup that follows it.
function releaseCall() {
  if (socket) socket.close();
  socket = null;
  if (micStream) micStream.getTracks().forEach((track) => track.stop());
  if (audioContext && audioContext.state !== 'closed') audioContext.close();
  setStatus('idle');
  const onEnded = onCallEnded;
  onCallEnded = null;
  if (onEnded) onEnded();
}

function setStatus(status) {
  const el = document.getElementById('status');
  if (el) el.textContent = status;
}

function startPlaybackStream() {
  audioEl = new Audio();
  mediaSource = new MediaSource();
  audioEl.src = URL.createObjectURL(mediaSource);
  pendingChunks = [];

  mediaSource.addEventListener('sourceopen', () => {
    sourceBuffer = mediaSource.addSourceBuffer('audio/mpeg');
    sourceBuffer.addEventListener('updateend', appendNextPendingChunk);
    appendNextPendingChunk();
  });

  audioEl.play().catch(() => {
    /* playback resumes once the user has interacted with the page */
  });
}

function appendAudioChunk(arrayBuffer) {
  if (!mediaSource) startPlaybackStream();
  pendingChunks.push(arrayBuffer);
  appendNextPendingChunk();
}

function appendNextPendingChunk() {
  if (!sourceBuffer || sourceBuffer.updating || pendingChunks.length === 0) return;
  sourceBuffer.appendBuffer(pendingChunks.shift());
}

function stopPlaybackStream() {
  if (audioEl) {
    audioEl.pause();
    audioEl.removeAttribute('src');
  }
  mediaSource = null;
  sourceBuffer = null;
  pendingChunks = [];
  audioEl = null;
}

function handleServerMessage(event) {
  if (typeof event.data === 'string') {
    const msg = JSON.parse(event.data);
    if (msg.type === 'transcript') appendTranscript(msg.role || 'user', msg.text);
    if (msg.type === 'state') setStatus(msg.state);
    if (msg.type === 'error') appendTranscript('error', msg.message);
    if (msg.type === 'latency') {
      const split = msg.stt_ms === undefined ? '' : ` (stt ${msg.stt_ms} · llm ${msg.llm_ms} · tts ${msg.tts_ms})`;
      appendTranscript('latency', `chờ ${msg.response_ms}ms${split}`);
    }
    if (msg.type === 'interrupt') player.stop();
    return;
  }
  player.play(event.data);
}

function appendTranscript(role, text) {
  const el = document.getElementById('transcript');
  if (!el) return;
  const line = document.createElement('div');
  line.textContent = `[${role}] ${text}`;
  el.appendChild(line);
}

// Not passed directly as the listener: startCall would take the click Event as its options.
document.getElementById('startButton')?.addEventListener('click', () => startCall());
document.getElementById('endButton')?.addEventListener('click', stopCall);
