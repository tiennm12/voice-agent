import re

PUNCTUATION_CHARS = '.!?;:,'
# Punctuation only counts as a sentence/clause boundary when followed by whitespace
# or the end of the currently-buffered text (more text may still arrive via push()).
# This keeps decimals/times like "8.5" or "12:30" from being split mid-number, since
# the punctuation there is immediately followed by a digit, not whitespace/end.
PUNCTUATION = re.compile(rf'[{PUNCTUATION_CHARS}](?=\s|\Z)')


class TextChunker:
    def __init__(self, on_flush, max_words_before_flush=8):
        self._on_flush = on_flush
        self._max_words = max(1, max_words_before_flush)  # clamp: 0 would never make progress
        self._buffer = ''

    def push(self, delta_text):
        self._buffer += delta_text

        while True:
            match = PUNCTUATION.search(self._buffer)
            if match:
                # Absorb consecutive punctuation ("?!") into the same chunk.
                end = match.end()
                while end < len(self._buffer) and self._buffer[end] in PUNCTUATION_CHARS:
                    end += 1
                chunk, self._buffer = self._buffer[:end].strip(), self._buffer[end:]
            else:
                words = self._buffer.split(None, self._max_words)
                if len(words) <= self._max_words:
                    return
                # The last element is the unsplit remainder, trailing whitespace intact.
                chunk, self._buffer = ' '.join(words[:-1]), words[-1]
            if chunk:
                self._on_flush(chunk)

    def end(self):
        remainder, self._buffer = self._buffer.strip(), ''
        if remainder:
            self._on_flush(remainder)
