const test = require('node:test');
const assert = require('node:assert/strict');
const { createLlmClient } = require('./llmClient');

function chunk(content) {
  return { choices: [{ delta: { content } }] };
}

test('streamCompletion calls onToken for each content delta, then onDone', async () => {
  const fakeOpenaiClient = {
    chat: {
      completions: {
        async create() {
          return (async function* () {
            yield chunk('Xin ');
            yield chunk('chào');
            yield { choices: [{ delta: {} }] }; // e.g. a role-only delta chunk
          })();
        },
      },
    },
  };

  const llmClient = createLlmClient({ openaiClient: fakeOpenaiClient });
  const tokens = [];
  let done = false;

  await new Promise((resolve) => {
    llmClient.streamCompletion([{ role: 'user', content: 'hi' }], {
      onToken: (t) => tokens.push(t),
      onDone: () => {
        done = true;
        resolve();
      },
      onError: () => resolve(),
    });
  });

  assert.deepEqual(tokens, ['Xin ', 'chào']);
  assert.equal(done, true);
});

test('abort() stops the stream before completion and does not call onDone/onError', async () => {
  let sawSignal;
  const fakeOpenaiClient = {
    chat: {
      completions: {
        async create(params, options) {
          sawSignal = options.signal;
          return (async function* () {
            for (const token of ['a', 'b', 'c']) {
              if (options.signal.aborted) {
                const err = new Error('aborted');
                err.name = 'AbortError';
                throw err;
              }
              yield chunk(token);
              await new Promise((r) => setImmediate(r));
            }
          })();
        },
      },
    },
  };

  const llmClient = createLlmClient({ openaiClient: fakeOpenaiClient });
  const tokens = [];
  let doneCalled = false;
  let errorCalled = false;

  const handle = llmClient.streamCompletion([{ role: 'user', content: 'hi' }], {
    onToken: (t) => {
      tokens.push(t);
      if (tokens.length === 1) handle.abort();
    },
    onDone: () => {
      doneCalled = true;
    },
    onError: () => {
      errorCalled = true;
    },
  });

  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.ok(sawSignal);
  assert.equal(sawSignal.aborted, true);
  assert.equal(doneCalled, false);
  assert.equal(errorCalled, false);
});
