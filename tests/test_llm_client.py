import asyncio
from types import SimpleNamespace

from voice_agent.llm_client import stream_completion


def chunk(content):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content))])


def fake_openai(make_stream):
    calls = []

    async def create(**params):
        calls.append(params)
        return make_stream()

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


async def test_stream_completion_yields_each_content_delta_and_skips_empty_ones():
    async def stream():
        yield chunk('Xin ')
        yield chunk('chào')
        yield chunk(None)  # e.g. a role-only delta chunk
        yield SimpleNamespace(choices=[])  # e.g. a usage-only chunk

    client, calls = fake_openai(stream)
    messages = [{'role': 'user', 'content': 'hi'}]

    tokens = [token async for token in stream_completion(client, messages)]

    assert tokens == ['Xin ', 'chào']
    assert calls == [{
        'model': 'gpt-4.1-mini', 'messages': messages, 'stream': True, 'stream_options': {'include_usage': True},
    }]


async def test_cancelling_the_consumer_closes_the_upstream_stream():
    closed = []

    async def stream():
        try:
            for token in ['a', 'b', 'c']:
                yield chunk(token)
                await asyncio.sleep(0)
        finally:
            closed.append(True)

    client, _ = fake_openai(stream)
    tokens = []

    async def consume():
        async for token in stream_completion(client, []):
            tokens.append(token)
            await asyncio.sleep(1)  # parked here when the cancel lands

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.01)  # let the generator finalizer run

    assert tokens == ['a']
    assert closed == [True]


async def test_stream_completion_reports_token_usage_from_the_final_chunk():
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=9)

    async def stream():
        yield chunk('Chào')
        yield SimpleNamespace(choices=[], usage=usage)

    client, _ = fake_openai(stream)
    reported = []

    tokens = [token async for token in stream_completion(client, [], on_usage=reported.append)]

    assert tokens == ['Chào']
    assert reported == [usage]
