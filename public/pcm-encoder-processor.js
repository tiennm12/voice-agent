// 480 samples @ 24kHz = 20ms of audio per frame. Buffering multiple process()
// callbacks' worth of samples (each callback is only 128 samples / ~5.33ms)
// avoids sending one WebSocket frame + one base64-encoded JSON message to
// OpenAI Realtime per callback, which roughly doubles payload size at that
// granularity and adds excessive per-frame overhead on a mobile uplink.
const FRAME_SAMPLES = 480;

class PcmEncoderProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buffer = new Int16Array(FRAME_SAMPLES);
    this._offset = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (input && input[0] && input[0].length > 0) {
      const floatSamples = input[0];
      for (let i = 0; i < floatSamples.length; i += 1) {
        const clamped = Math.max(-1, Math.min(1, floatSamples[i]));
        this._buffer[this._offset] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
        this._offset += 1;
        if (this._offset >= FRAME_SAMPLES) {
          // Transfer ownership of the underlying buffer (zero-copy) and start a
          // fresh Int16Array for the next frame.
          this.port.postMessage(this._buffer.buffer, [this._buffer.buffer]);
          this._buffer = new Int16Array(FRAME_SAMPLES);
          this._offset = 0;
        }
      }
    }
    // Leftover partial frame (< FRAME_SAMPLES) intentionally stays buffered until
    // the next process() call tops it off — this is a continuous stream, not a
    // one-shot file, so there's no "final flush" needed.
    return true;
  }
}

registerProcessor('pcm-encoder-processor', PcmEncoderProcessor);
