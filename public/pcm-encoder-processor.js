class PcmEncoderProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0] && input[0].length > 0) {
      const floatSamples = input[0];
      const int16Samples = new Int16Array(floatSamples.length);
      for (let i = 0; i < floatSamples.length; i += 1) {
        const clamped = Math.max(-1, Math.min(1, floatSamples[i]));
        int16Samples[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      }
      this.port.postMessage(int16Samples.buffer, [int16Samples.buffer]);
    }
    return true;
  }
}

registerProcessor('pcm-encoder-processor', PcmEncoderProcessor);
