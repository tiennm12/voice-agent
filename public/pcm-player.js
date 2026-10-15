const PCM_SAMPLE_RATE = 24000; // the server's 'pcm' audio format: 16-bit mono at 24kHz

// Plays the agent's raw PCM straight into the call through Web Audio. Deliberately no
// <audio> element: Chrome defers media-element loading in a background tab, and this
// tab is meant to be left in the background.
function createCallPlayer(context, destination) {
  let playhead = 0; // context time at which the next chunk starts
  let carry = null; // a chunk's odd trailing byte: half of a sample split across chunks
  const scheduled = new Set();

  return {
    play(arrayBuffer) {
      let bytes = new Uint8Array(arrayBuffer);
      if (carry !== null) {
        const joined = new Uint8Array(bytes.length + 1);
        joined[0] = carry;
        joined.set(bytes, 1);
        bytes = joined;
        carry = null;
      }
      if (bytes.length % 2) {
        carry = bytes[bytes.length - 1];
        bytes = bytes.subarray(0, bytes.length - 1);
      }
      if (bytes.length === 0) return;

      const samples = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      const buffer = context.createBuffer(1, bytes.length / 2, PCM_SAMPLE_RATE);
      const channel = buffer.getChannelData(0);
      for (let i = 0; i < channel.length; i += 1) {
        channel[i] = samples.getInt16(i * 2, true) / 0x8000;
      }

      const node = context.createBufferSource();
      node.buffer = buffer;
      node.connect(destination);
      // Chunks arrive faster than real time: queue each right behind the previous
      // one, with a small cushion when starting from silence.
      playhead = Math.max(playhead, context.currentTime + 0.05);
      node.start(playhead);
      playhead += buffer.duration;
      scheduled.add(node);
      node.onended = () => scheduled.delete(node);
    },
    stop() {
      scheduled.forEach((node) => node.stop());
      scheduled.clear();
      playhead = 0;
      carry = null;
    },
  };
}
