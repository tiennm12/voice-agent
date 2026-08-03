let audioContext;
let micStream;
let socket;

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

function handleServerMessage(event) {
  // Playback handling added in Task 12.
  if (typeof event.data === 'string') {
    const msg = JSON.parse(event.data);
    if (msg.type === 'transcript') appendTranscript('user', msg.text);
    if (msg.type === 'error') appendTranscript('error', msg.message);
  }
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
