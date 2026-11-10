"""模型协议测试：流式工具参数、思考字段回传和错误脱敏。"""
import json

import httpx
import pytest

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
