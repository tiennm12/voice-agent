# The model that talks to the caller and writes the call summary, and the reasoning effort
# it runs with. gpt-6-luna reasons by default, which puts seconds of thinking before the
# first word of every reply; 'none' switches that off. The two belong together: a model
# without reasoning (gpt-4.1-mini, gpt-4o-mini) rejects the parameter, so its effort is None.
# config/pricing.json holds this model's prices.
CHAT_MODEL = 'gpt-6-luna'
CHAT_REASONING_EFFORT = 'none'


async def stream_completion(
    openai_client, messages, model=CHAT_MODEL, on_usage=None, reasoning_effort=CHAT_REASONING_EFFORT,
):
    """Yield content tokens as they arrive. Cancel the consuming task to abort.

    on_usage, if given, receives the token usage object the API sends as its last chunk.
    Pass reasoning_effort=None together with a model that has no reasoning.
    """
    params = {'model': model, 'messages': messages, 'stream': True, 'stream_options': {'include_usage': True}}
    if reasoning_effort:
        params['reasoning_effort'] = reasoning_effort
    stream = await openai_client.chat.completions.create(**params)
    try:
        async for chunk in stream:
            if on_usage and getattr(chunk, 'usage', None):
                on_usage(chunk.usage)
            token = chunk.choices[0].delta.content if chunk.choices else None
            if token:
                yield token
    finally:
        await stream.aclose()
