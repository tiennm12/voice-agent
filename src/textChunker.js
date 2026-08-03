const PUNCTUATION = /[.!?;:,]/;

function wordCount(text) {
  const trimmed = text.trim();
  return trimmed === '' ? 0 : trimmed.split(/\s+/).length;
}

function createTextChunker({ onFlush, maxWordsBeforeFlush = 8 }) {
  let buffer = '';
  const effectiveMax = Math.max(1, maxWordsBeforeFlush); // Clamp to minimum 1 to prevent infinite loops

  function push(deltaText) {
    buffer += deltaText;

    let progressed = true;
    while (progressed) {
      progressed = false;

      const match = buffer.match(PUNCTUATION);
      if (match) {
        const idx = match.index;
        // Absorb consecutive punctuation into same chunk to avoid degenerate single-char flushes
        let endIdx = idx + 1;
        while (endIdx < buffer.length && PUNCTUATION.test(buffer[endIdx])) {
          endIdx++;
        }
        const chunk = buffer.slice(0, endIdx).trim();
        buffer = buffer.slice(endIdx);
        if (chunk.length > 0) onFlush(chunk);
        progressed = true;
        continue;
      }

      if (wordCount(buffer) > effectiveMax) {
        const trimmed = buffer.trim();
        const words = trimmed.split(/\s+/);
        if (words.length <= effectiveMax) continue; // shouldn't happen but be defensive
        const chunk = words.slice(0, effectiveMax).join(' ');
        buffer = words.slice(effectiveMax).join(' ');
        if (chunk.length > 0) onFlush(chunk);
        progressed = true;
      }
    }
  }

  function end() {
    const remainder = buffer.trim();
    buffer = '';
    if (remainder.length > 0) onFlush(remainder);
  }

  return { push, end };
}

module.exports = { createTextChunker };
