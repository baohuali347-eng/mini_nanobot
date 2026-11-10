"""真实本地 HTTP 验证：认证、会话、流式回复、记忆与停止任务。"""
import asyncio
import json

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from mini_nanobot.agent import AgentService
from mini_nanobot.server import create_app


async def test_authenticated_http_chat_memory_and_session_reload(settings, model_factory):
    model = model_factory([{'role': 'assistant', 'content': '精简版运行正常。'}])
    service = AgentService(settings, model)
    client = TestClient(TestServer(create_app(settings, service, 'test-access')), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        assert (await client.get('/api/status')).status == 401
        assert (await client.post('/api/login', json={'token': 'wrong'})).status == 401
        assert (await client.post('/api/login', json={'token': 'test-access'})).status == 200
        assert (await client.get('/api/channels')).status == 404
        assert (await client.post('/api/sessions', json={}, headers={'Origin': 'https://evil.example'})).status == 403
        response = await client.post('/api/sessions', json={'workspace': str(settings.workspace)})
        assert response.status == 201
        item = await response.json()
        identity = item['id']
        response = await client.post(f'/api/sessions/{identity}/chat', json={'message': '验证页面链路'})
        events = [json.loads(line) for line in (await response.text()).splitlines()]
        assert any(event.get('text') == '精简版运行正常。' for event in events)
        assert events[-1]['type'] == 'done'
        assert events[-1]['session']['status'] == 'idle'
        await client.put(f'/api/sessions/{identity}/memory', json={'content': '本项目使用 pytest。'})
        memory = await (await client.get(f'/api/sessions/{identity}/memory')).json()
        assert memory['content'] == '本项目使用 pytest。'
        snapshot = await (await client.get(f'/api/sessions/{identity}')).json()
        assert snapshot['messages'][-1]['content'] == '精简版运行正常。'
        assert 'test-only-key' not in json.dumps(snapshot)
    finally:
        await client.close()


async def test_stop_endpoint_completes_running_stream(settings, model_factory):
    entered = asyncio.Event()
    async def wait_forever(*args):
        entered.set()
        await asyncio.Event().wait()
    service = AgentService(settings, model_factory([wait_forever]))
    client = TestClient(TestServer(create_app(settings, service, 'test-access')), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        await client.post('/api/login', json={'token': 'test-access'})
        item = await (await client.post('/api/sessions', json={'workspace': str(settings.workspace)})).json()
        response = await client.post(f"/api/sessions/{item['id']}/chat", json={'message': '运行后停止'})
        await entered.wait()
        assert (await client.post(f"/api/sessions/{item['id']}/stop")).status == 200
        events = [json.loads(line) for line in (await response.text()).splitlines()]
        assert events[-1]['session']['status'] == 'cancelled'
    finally:
        await client.close()


async def test_plan_dependencies_and_handoff_survive_http_reload(settings, model_factory, call):
    plan = [
        {'id': 'explore', 'role': 'explorer', 'task': '定位问题', 'depends_on': [], 'acceptance': ['找到问题依据']},
        {'id': 'implement', 'role': 'coder', 'task': '实现', 'depends_on': ['explore'], 'acceptance': ['完成修复']},
    ]
    model = model_factory([
        call('subagent', action='plan', tasks=plan),
        {'role': 'assistant', 'content': json.dumps({'outcome': 'needs_followup', 'summary': '缺少代码文件', 'unresolved': ['请提供代码目录']})},
        {'role': 'assistant', 'content': '探索待处理，实现已阻断。'},
    ])
    service = AgentService(settings, model)
    client = TestClient(TestServer(create_app(settings, service, 'test-access')), cookie_jar=aiohttp.CookieJar(unsafe=True))
    await client.start_server()
    try:
        await client.post('/api/login', json={'token': 'test-access'})
        session = await (await client.post('/api/sessions', json={'workspace': str(settings.workspace)})).json()
        identity = session['id']
        response = await client.post(f'/api/sessions/{identity}/chat', json={'message': '使用任务计划'})
        streamed = [json.loads(line) for line in (await response.text()).splitlines()]
        blocked = next(event for event in streamed if event.get('state') == 'blocked')
        assert blocked['depends_on'] == ['explore']
        assert blocked['handoff']['checks'] == []
        assert blocked['run_id']
        snapshot = await (await client.get(f'/api/sessions/{identity}')).json()
        assert blocked in snapshot['events']
        assert len(model.requests) == 3  # 主任务两次、探索一次；实现没有调用模型。
    finally:
        await client.close()
