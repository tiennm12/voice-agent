function createLlmClient({ openaiClient }) {
  function streamCompletion(messages, { model = 'gpt-4.1-mini', onToken, onDone, onError }) {
    const controller = new AbortController();

    (async () => {
      try {
        const stream = await openaiClient.chat.completions.create(
          { model, messages, stream: true },
          { signal: controller.signal },
        );
        for await (const chunk of stream) {
          const token = chunk.choices && chunk.choices[0] && chunk.choices[0].delta && chunk.choices[0].delta.content;
          if (token) onToken(token);
        }
        onDone();
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        onError(err);
      }
    })();

    return { abort: () => controller.abort() };
  }

  return { streamCompletion };
}

module.exports = { createLlmClient };
