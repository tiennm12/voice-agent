function buildMessages({ systemPrompt, history, newUserText }) {
  return [
    { role: 'system', content: systemPrompt },
    ...history,
    { role: 'user', content: newUserText },
  ];
}

module.exports = { buildMessages };
