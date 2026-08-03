let audioContext;
let micStream;
let socket;
let mediaSource;
let sourceBuffer;
let audioEl;
let pendingChunks = [];

async function startCall() {
  audioContext = new AudioContext({ sampleRate: 24000 });
  await audioContext.audioWorklet.addModule('pcm-encoder-processor.js');

  micStream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
  const source = audioContext.createMediaStreamSource(micStream);
  const encoderNode = new AudioWorkletNode(audioContext, 'pcm-encoder-processor');
  source.connect(encoderNode);

  socket = new WebSocket(`ws://${location.host}/ws`);
  socket.binaryType = 'arraybuffer';

  encoderNode.port.onmessage = (event) => {
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(event.data);
    }
  };

  socket.addEventListener('open', () => setStatus('listening'));
  socket.addEventListener('close', () => setStatus('idle'));
  socket.addEventListener('message', handleServerMessage);
}

function stopCall() {
  if (socket) socket.close();
  if (micStream) micStream.getTracks().forEach((track) => track.stop());
  if (audioContext) audioContext.close();
  setStatus('idle');
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
    if (msg.type === 'transcript') appendTranscript('user', msg.text);
    if (msg.type === 'state') setStatus(msg.state);
    if (msg.type === 'error') appendTranscript('error', msg.message);
    if (msg.type === 'interrupt') stopPlaybackStream();
    return;
  }
  appendAudioChunk(event.data);
}

function appendTranscript(role, text) {
  const el = document.getElementById('transcript');
  if (!el) return;
  const line = document.createElement('div');
  line.textContent = `[${role}] ${text}`;
  el.appendChild(line);
}

document.getElementById('startButton').addEventListener('click', startCall);
document.getElementById('endButton').addEventListener('click', stopCall);
