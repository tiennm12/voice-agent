const PUNCTUATION = /[.!?;:,]/;

function wordCount(text) {
  const trimmed = text.trim();
  return trimmed === '' ? 0 : trimmed.split(/\s+/).length;
}

function createTextChunker({ onFlush, maxWordsBeforeFlush = 8 }) {
  let buffer = '';

  function push(deltaText) {
    buffer += deltaText;

    let progressed = true;
    while (progressed) {
      progressed = false;

      const match = buffer.match(PUNCTUATION);
      if (match) {
        const idx = match.index;
        const chunk = buffer.slice(0, idx + 1).trim();
        buffer = buffer.slice(idx + 1);
        if (chunk.length > 0) onFlush(chunk);
        progressed = true;
        continue;
      }

      if (wordCount(buffer) > maxWordsBeforeFlush) {
        const trimmed = buffer.trim();
        const words = trimmed.split(/\s+/);
        if (words.length <= maxWordsBeforeFlush) continue; // shouldn't happen but be defensive
        const chunk = words.slice(0, maxWordsBeforeFlush).join(' ');
        buffer = words.slice(maxWordsBeforeFlush).join(' ');
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
