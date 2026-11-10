"""模型协议测试：流式工具参数、思考字段回传和错误脱敏。"""
import json

import httpx
import pytest

from mini_nanobot.agent import AgentService
from mini_nanobot.context import compact
from mini_nanobot.events import event_scope
from mini_nanobot.provider import ModelProvider


async def test_stream_assembles_tool_calls_and_preserves_reasoning(settings):
    packets = [
        {'delta': {'reasoning_content': '检查代码'}},
        {'delta': {'tool_calls': [{'index': 0, 'id': 'call-1', 'function': {'name': 'read_file', 'arguments': '{"path":'}}]}},
        {'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '"app.py"}'}}]}},
        {'delta': {}, 'finish_reason': 'tool_calls'},
    ]
    def respond(request):
        assert request.headers['authorization'] == 'Bearer test-only-key'
        assert json.loads(request.content)['stream'] is True
        content = ''.join('data: ' + json.dumps({'choices': [packet]}) + '\n\n' for packet in packets)
        return httpx.Response(200, text=content + 'data: [DONE]\n\n')
    provider = ModelProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        answer = await provider.complete([{'role': 'user', 'content': '读文件'}])
        assert answer['reasoning_content'] == '检查代码'
        assert json.loads(answer['tool_calls'][0]['function']['arguments']) == {'path': 'app.py'}
    finally:
        await provider.close()


async def test_authentication_failure_does_not_echo_key(settings):
    provider = ModelProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401)))
    try:
        with pytest.raises(RuntimeError) as error:
            await provider.complete([{'role': 'user', 'content': '你好'}])
        assert settings.api_key not in str(error.value)
        assert '401' in str(error.value)
    finally:
        await provider.close()


@pytest.mark.parametrize('usage', [None, {'prompt_tokens': 8, 'completion_tokens': 3, 'total_tokens': 11}])
async def test_usage_only_packet_is_traced_without_polluting_model_messages(settings, usage):
    events = []
    async def emit(event):
        events.append(event)
    def respond(request):
        payload = json.loads(request.content)
        assert payload['stream_options'] == {'include_usage': True}
        assert all('_span_id' not in message for message in payload['messages'])
        packets = [{'choices': [{'delta': {'content': '你好'}}]},
                   {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}]
        if usage is not None:
            packets.append({'choices': [], 'usage': usage})
        return httpx.Response(200, text=''.join('data: ' + json.dumps(packet) + '\n\n' for packet in packets))
    provider = ModelProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with event_scope(run_id='run-a', task_id='child-a', role='coder', parent_span_id='tool-a'):
            await provider.complete([{'role': 'user', 'content': '你好'}], emit=emit)
    finally:
        await provider.close()
    completed = next(event for event in events if event['type'] == 'model' and event['state'] == 'done')
    assert completed['usage'] == usage
    assert completed['usage_source'] == ('provider' if usage else 'unavailable')
    assert completed['run_id'] == 'run-a' and completed['parent_span_id'] == 'tool-a'
    assert completed['elapsed_ms'] >= 0 and completed['first_token_ms'] >= 0
    assert settings.api_key not in json.dumps(events)


async def test_retries_are_separate_requests_in_one_model_span(settings, monkeypatch):
    events, calls = [], []
    async def emit(event):
        events.append(event)
    async def no_sleep(seconds):
        pass
    monkeypatch.setattr('mini_nanobot.provider.asyncio.sleep', no_sleep)
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"恢复"},"finish_reason":"stop"}]}\n\n')
    provider = ModelProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        await provider.complete([{'role': 'user', 'content': '你好'}], emit=emit)
    finally:
        await provider.close()
    starts = [event for event in events if event.get('state') == 'running']
    assert len(starts) == 2 and starts[0]['request_id'] != starts[1]['request_id']
    assert starts[0]['span_id'] == starts[1]['span_id']
    assert any(event.get('retrying') for event in events)


async def test_chat_persists_nested_trace_and_never_sends_trace_fields_to_model(settings):
    def respond(request):
        payload = json.loads(request.content)
        assert all(not any(key.startswith('_') for key in message) for message in payload['messages'])
        if '你是探索 agent' in payload['messages'][0]['content']:
            delta, finish = {'content': '已完成只读探索。'}, 'stop'
        elif payload['messages'][-1]['role'] == 'tool':
            delta, finish = {'content': '已核对探索结果。'}, 'stop'
        else:
            delta, finish = {'tool_calls': [{'index': 0, 'id': 'delegate', 'function': {
                'name': 'subagent', 'arguments': json.dumps({'action': 'create', 'role': 'explorer', 'task': '只读探索', 'wait': True})}}]}, 'tool_calls'
        packets = [{'choices': [{'delta': delta}]}, {'choices': [{'delta': {}, 'finish_reason': finish}]}]
        return httpx.Response(200, text=''.join('data: ' + json.dumps(packet) + '\n\n' for packet in packets))
    provider = ModelProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    service = AgentService(settings, provider)
    try:
        session = service.create_session(str(settings.workspace))
        await service.start(session['id'], '安排一个探索任务')
        saved = service.snapshot(session['id'])
        assert saved['status'] == 'idle'
        models = [event for event in saved['events'] if event['type'] == 'model' and event['state'] == 'done']
        assert {event['role'] for event in models} == {'main', 'explorer'}
        assert len({event['run_id'] for event in models}) == 1
        tool = next(event for event in saved['events'] if event['type'] == 'tool' and event['state'] == 'done')
        child = next(event for event in models if event['role'] == 'explorer')
        assert child['parent_span_id'] == tool['span_id']
        assert child['parent_task_id'] == 'main'
        assert tool['parent_span_id'] in {event['span_id'] for event in models if event['role'] == 'main'}
    finally:
        await service.close()


async def test_compaction_is_traced_without_streaming_internal_summary(settings):
    events = []
    async def emit(event):
        events.append(event)
    def respond(request):
        packets = [{'choices': [{'delta': {'content': '内部任务摘要'}, 'finish_reason': 'stop'}]},
                   {'choices': [], 'usage': {'prompt_tokens': 30, 'completion_tokens': 6, 'total_tokens': 36}}]
        return httpx.Response(200, text=''.join('data: ' + json.dumps(packet) + '\n\n' for packet in packets))
    provider = ModelProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    history = [{'role': 'user', 'content': '最初任务'}] + [{'role': 'assistant', 'content': '旧记录' * 600} for _ in range(8)]
    history.append({'role': 'user', 'content': '当前任务'})
    try:
        remaining, summary = await compact(provider, history, '', 500, emit)
    finally:
        await provider.close()
    assert summary == '内部任务摘要' and remaining[-1]['content'] == '当前任务'
    assert not any(event['type'] == 'text' for event in events)
    usage = next(event for event in events if event['type'] == 'model' and event['state'] == 'done')
    assert usage['purpose'] == 'compaction' and usage['usage']['total_tokens'] == 36


async def test_usage_request_can_be_disabled_for_compatible_services(settings):
    settings.stream_usage = False
    def respond(request):
        assert 'stream_options' not in json.loads(request.content)
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"正常回复"},"finish_reason":"stop"}]}\n\n')
    provider = ModelProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        answer = await provider.complete([{'role': 'user', 'content': '你好'}])
        assert answer['content'] == '正常回复'
    finally:
        await provider.close()


async def test_empty_reasoning_is_preserved_for_next_tool_round(settings):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            delta = {'reasoning_content': '', 'tool_calls': [{'index': 0, 'id': 'read-1',
                     'function': {'name': 'read_file', 'arguments': '{"path":"app.py"}'}}]}
            finish = 'tool_calls'
        else:
            assert payload['messages'][1]['reasoning_content'] == ''
            delta, finish = {'content': '已读取。'}, 'stop'
        return httpx.Response(200, text='data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': finish}]}) + '\n\n')

    provider = ModelProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        messages = [{'role': 'user', 'content': '读取文件'}]
        answer = await provider.complete(messages)
        messages.extend([{key: value for key, value in answer.items() if not key.startswith('_')},
                         {'role': 'tool', 'tool_call_id': 'read-1', 'content': '代码'}])
        assert (await provider.complete(messages))['content'] == '已读取。'
    finally:
        await provider.close()


async def test_output_limit_and_redacted_api_error_are_reported(settings):
    settings.max_output_tokens = 8192

    def respond(request):
        assert json.loads(request.content)['max_tokens'] == 8192
        return httpx.Response(400, json={'error': {'message': 'Missing reasoning_content ' + settings.api_key}})

    provider = ModelProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(RuntimeError) as error:
            await provider.complete([{'role': 'user', 'content': '修复代码'}])
        assert 'Missing reasoning_content' in str(error.value)
        assert settings.api_key not in str(error.value)
    finally:
        await provider.close()
