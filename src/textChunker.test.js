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
