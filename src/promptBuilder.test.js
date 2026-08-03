// src/promptBuilder.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { buildMessages } = require('./promptBuilder');

test('puts the system prompt first, then history, then the new user turn', () => {
  const messages = buildMessages({
    systemPrompt: 'SYS',
    history: [
      { role: 'user', content: 'Chào' },
      { role: 'assistant', content: 'Chào bạn' },
    ],
    newUserText: 'Giờ mở cửa lúc mấy giờ?',
  });

  assert.deepEqual(messages, [
    { role: 'system', content: 'SYS' },
    { role: 'user', content: 'Chào' },
    { role: 'assistant', content: 'Chào bạn' },
    { role: 'user', content: 'Giờ mở cửa lúc mấy giờ?' },
  ]);
});

test('works with empty history', () => {
  const messages = buildMessages({ systemPrompt: 'SYS', history: [], newUserText: 'Hi' });
  assert.deepEqual(messages, [
    { role: 'system', content: 'SYS' },
    { role: 'user', content: 'Hi' },
  ]);
});
