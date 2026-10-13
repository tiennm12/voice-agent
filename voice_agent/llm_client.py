CHAT_MODEL = 'gpt-4.1-mini'


async def stream_completion(openai_client, messages, model=CHAT_MODEL, on_usage=None):
    """Yield content tokens as they arrive. Cancel the consuming task to abort.

    on_usage, if given, receives the token usage object the API sends as its last chunk.
    """
    stream = await openai_client.chat.completions.create(
        model=model, messages=messages, stream=True, stream_options={'include_usage': True},
    )
    try:
        async for chunk in stream:
            if on_usage and getattr(chunk, 'usage', None):
                on_usage(chunk.usage)
            token = chunk.choices[0].delta.content if chunk.choices else None
            if token:
                yield token
    finally:
        await stream.aclose()
