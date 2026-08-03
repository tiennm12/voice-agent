const test = require('node:test');
const assert = require('node:assert/strict');
const { createTextChunker } = require('./textChunker');

test('flushes a chunk when punctuation arrives, split across multiple pushes', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c) });
  chunker.push('Xin ');
  chunker.push('chào');
  chunker.push('.');
  chunker.push(' Bạn');
  assert.deepEqual(flushed, ['Xin chào.']);
});

test('flushes multiple punctuation-bounded chunks from one push', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c) });
  chunker.push('Chào bạn, hôm nay trời đẹp. Bạn khỏe không?');
  assert.deepEqual(flushed, ['Chào bạn,', 'hôm nay trời đẹp.', 'Bạn khỏe không?']);
});

test('falls back to a word-count flush when no punctuation appears', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c), maxWordsBeforeFlush: 4 });
  chunker.push('một hai ba bốn năm sáu');
  assert.deepEqual(flushed, ['một hai ba bốn']);
});

test('end() flushes any remaining buffered text', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c) });
  chunker.push('không có dấu câu');
  chunker.end();
  assert.deepEqual(flushed, ['không có dấu câu']);
});

test('end() does not flush an empty or whitespace-only buffer', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c) });
  chunker.push('   ');
  chunker.end();
  assert.deepEqual(flushed, []);
});

test('does not hang when maxWordsBeforeFlush is 0 (regression)', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c), maxWordsBeforeFlush: 0 });
  chunker.push('một hai ba');
  // Should flush word-by-word (as if maxWordsBeforeFlush were 1, clamped from 0)
  // This test verifies no infinite loop occurs; exact flushing depends on clamping behavior
  assert.deepEqual(flushed, ['một', 'hai']);
});

test('absorbs back-to-back punctuation into single chunk', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c) });
  chunker.push('Hello?!');
  // Should produce one chunk, not separate ['Hello?', '!']
  assert.deepEqual(flushed, ['Hello?!']);
});

test('absorbs back-to-back punctuation followed by trailing text into a single chunk', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c) });
  chunker.push('Hello?! Bạn khỏe không?');
  // "?!" should stay merged (not split into 'Hello?' and '!'), and the following
  // sentence should flush separately at its own trailing '?'.
  assert.deepEqual(flushed, ['Hello?!', 'Bạn khỏe không?']);
});

test('does not split a decimal number at the punctuation boundary', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c) });
  chunker.push('Giá khoảng 8.5 triệu đồng.');
  // The '.' inside "8.5" is followed by a digit, not whitespace, so it must not
  // be treated as a sentence boundary; only the trailing '.' should flush.
  assert.deepEqual(flushed, ['Giá khoảng 8.5 triệu đồng.']);
});

test('does not split a decimal number even when more punctuation-bearing text follows', () => {
  const flushed = [];
  const chunker = createTextChunker({ onFlush: (c) => flushed.push(c) });
  chunker.push('Giá 8.5 triệu, đã bao gồm thuế.');
  assert.deepEqual(flushed, ['Giá 8.5 triệu,', 'đã bao gồm thuế.']);
});
