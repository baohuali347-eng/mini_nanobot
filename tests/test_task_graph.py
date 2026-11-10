"""依赖计划的行为契约：整批校验、真实交接、并发与失败传播。"""
import asyncio
import json
import shlex
import sys

import pytest

from mini_nanobot.agent import AgentService
from mini_nanobot.runner import AgentRunner
from mini_nanobot.session import SessionStore
from mini_nanobot.subagents import SubagentManager
from mini_nanobot.tools.registry import build_tools


def spec(identity, role='explorer', dependencies=()):
    return {'id': identity, 'role': role, 'task': identity,
            'depends_on': list(dependencies), 'acceptance': ['提供实际依据']}


def report(summary='已完成', outcome='completed', unresolved=None):
    return {'role': 'assistant', 'content': json.dumps({
        'outcome': outcome, 'summary': summary, 'unresolved': unresolved or [],
    }, ensure_ascii=False)}


@pytest.fixture
async def make_manager(settings, model_factory):
    managers = []
    store = SessionStore(settings.home)
    lock = asyncio.Lock()
    def make(responses, slots=2):
        events = []
        async def emit(event):
            events.append(event)
        model = model_factory(responses)
        manager = SubagentManager(AgentRunner(model, settings), '测试编程助手',
            lambda role: build_tools(settings.workspace, settings, store, lock, role), emit, asyncio.Semaphore(slots))
        managers.append(manager)
        return manager, events, model
    yield make
    for manager in managers:
        await manager.close()
    store.close()


@pytest.mark.parametrize('tasks', [
    [spec('a', dependencies=['missing'])],
    [spec('a'), spec('a')],
    [spec('a', dependencies=['b']), spec('b', dependencies=['a'])],
    [spec('a', dependencies=['a'])],
    [dict(spec('a'), acceptance=[])],
])
async def test_invalid_plan_has_no_partial_execution(make_manager, tasks):
    manager, events, model = make_manager([])
    with pytest.raises(ValueError):
        await manager.dispatch('plan', tasks=tasks)
    assert not manager.tasks and not events and not model.requests


async def test_dependency_order_handoff_and_persistence(settings, model_factory, call):
    source = settings.workspace / 'calculator.py'
    source.write_text('def add(a, b):\n    return a - b\n')
    # 下游放在前面，且只有一个名额；等待依赖不能占用名额。
    plan = [spec('review', 'reviewer', ['implement']), spec('implement', 'coder', ['explore']), spec('explore')]
    command = shlex.quote(sys.executable) + " -c 'from calculator import add; assert add(2,3)==5; print(\"断言通过\")'"
    model = model_factory([
        call('subagent', action='plan', tasks=plan),
        call('read_file', path='calculator.py'), report('定位到减法错误'),
        call('edit_file', path='calculator.py', old='a - b', new='a + b'),
        call('exec', command=command), report('修正加法，实际断言通过'),
        call('read_file', path='calculator.py'), report('复核源码与前置执行证据'),
        call('subagent', action='check', task_id='implement'),
        {'role': 'assistant', 'content': '根据交接完成汇报。'},
    ])
    service = AgentService(settings, model)
    try:
        session = service.create_session(str(settings.workspace))
        await asyncio.wait_for(service.start(session['id'], '按计划修复加法并审查'), 5)
        saved = service.snapshot(session['id'])
        assert saved['status'] == 'idle'
        completed = [event for event in saved['events'] if event['type'] == 'task' and event['state'] == 'done']
        assert [event['task_id'] for event in completed] == ['explore', 'implement', 'review']
        handoff = completed[1]['handoff']
        assert handoff['files_changed'] == ['calculator.py']
        assert handoff['checks'][0]['exit_code'] == 0
        assert handoff['checks'][0]['timed_out'] is False
        assert '断言通过' in handoff['checks'][0]['output']
        assert handoff['reported_outcome'] == 'completed'
        assert source.read_text().endswith('return a + b\n')
        reviewer_prompt = model.requests[6]
        inherited = json.loads(reviewer_prompt[1]['content'].split('\n', 1)[1])
        assert inherited == handoff
        # 主 agent 的 check 也能读取完整交接，而非仅获得概览。
        assert json.loads(model.requests[-1][-1]['content']) == handoff
        assert completed[2]['depends_on'] == ['implement']
        assert completed[2]['acceptance'] == ['提供实际依据']
        identity = session['id']
    finally:
        await service.close()
    reopened = SessionStore(settings.home)
    try:
        assert any(event.get('handoff') == handoff for event in reopened.events(identity))
    finally:
        reopened.close()


async def test_ready_nodes_run_concurrently_and_join_waits(make_manager):
    gate, two_started = asyncio.Event(), asyncio.Event()
    running, peak = 0, 0
    completed = set()
    async def child(messages, tools, emit):
        nonlocal running, peak
        identity = messages[-1]['content'].split('\n')[0]
        if identity == 'join':
            assert {'a', 'b', 'c'} <= completed
        running += 1
        peak = max(peak, running)
        if running == 2:
            two_started.set()
        await gate.wait()
        running -= 1
        completed.add(identity)
        return report(identity)
    manager, events, _ = make_manager([child] * 4, slots=2)
    await manager.dispatch('plan', tasks=[spec('join', dependencies=['a', 'b', 'c']), spec('a'), spec('b'), spec('c')], wait=False)
    await asyncio.wait_for(two_started.wait(), 2)
    assert manager.tasks['join'].status == 'waiting'
    assert manager.tasks['c'].status == 'queued'
    gate.set()
    await asyncio.wait_for(manager.notifications(wait=True), 2)
    assert peak == 2 and all(item.status == 'done' for item in manager.tasks.values())
    assert any(event['state'] == 'waiting' for event in events)


@pytest.mark.parametrize('response, expected', [
    (report('无法完成', 'needs_followup', ['缺少需求']), 'needs_followup'),
    (report('仍有问题', 'completed', ['未解决异常']), 'needs_followup'),
    ({'role': 'assistant', 'content': '不是约定的交接格式'}, 'error'),
    ({'role': 'assistant', 'content': '{"outcome":"completed","summary":"通过","unresolved":[],"checks":[{"exit_code":0}]}'}, 'error'),
])
async def test_incomplete_or_invalid_report_blocks_all_descendants(make_manager, response, expected):
    manager, events, model = make_manager([response])
    await asyncio.wait_for(manager.dispatch('plan', tasks=[spec('c', dependencies=['b']), spec('b', dependencies=['a']), spec('a')]), 2)
    assert manager.tasks['a'].status == expected
    assert manager.tasks['b'].status == manager.tasks['c'].status == 'blocked'
    assert len(model.requests) == 1
    assert not manager.tasks['a'].checks
    assert not any(event['task_id'] in {'b', 'c'} and event['state'] == 'running' for event in events)


async def test_cancelled_predecessor_blocks_waiter_without_cancelling_plan(make_manager):
    entered = asyncio.Event()
    async def child(*args):
        entered.set()
        await asyncio.Event().wait()
    manager, _, model = make_manager([child])
    plan = asyncio.create_task(manager.dispatch('plan', tasks=[spec('b', dependencies=['a']), spec('a')]))
    await asyncio.wait_for(entered.wait(), 2)
    await manager.dispatch('cancel', task_id='a')
    result = json.loads(await asyncio.wait_for(plan, 2))
    assert {item['status'] for item in result['tasks']} == {'cancelled', 'blocked'}
    assert len(model.requests) == 1


async def test_close_cancels_active_and_waiting_nodes(make_manager):
    entered = asyncio.Event()
    async def child(*args):
        entered.set()
        await asyncio.Event().wait()
    manager, _, _ = make_manager([child])
    await manager.dispatch('plan', tasks=[spec('b', dependencies=['a']), spec('a')], wait=False)
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(manager.close(), 2)
    assert all(item.finished.is_set() and item.execution.done() for item in manager.tasks.values())
    assert all(item.status == 'cancelled' for item in manager.tasks.values())


async def test_immediate_cancel_and_claimed_tests_are_not_execution_evidence(make_manager):
    manager, _, model = make_manager([report('我已测试通过并修改 fake.py')])
    await manager.dispatch('plan', tasks=[spec('a')], wait=False)
    await manager.dispatch('cancel', task_id='a')
    assert not model.requests and manager.tasks['a'].finished.is_set()
    await manager.dispatch('plan', tasks=[spec('b')])
    handoff = json.loads(await manager.dispatch('check', task_id='b'))
    assert handoff['summary'] == '我已测试通过并修改 fake.py'
    assert handoff['checks'] == [] and handoff['files_changed'] == []


async def test_failed_commands_preserve_real_exit_code(make_manager, call):
    manager, events, _ = make_manager([call('exec', command='exit 7'), report('命令失败', 'needs_followup', ['退出码非零'])])
    await manager.dispatch('plan', tasks=[spec('a', 'coder')])
    assert manager.tasks['a'].checks[0]['exit_code'] == 7
    assert any(event.get('name') == 'exec' and event['state'] == 'error' for event in events)


async def test_timeout_is_recorded_as_timeout_not_pass(make_manager, settings, call):
    settings.command_timeout = 1
    manager, _, _ = make_manager([call('exec', command='sleep 5'), report('验证超时', 'needs_followup', ['验证未完成'])])
    await asyncio.wait_for(manager.dispatch('plan', tasks=[spec('a', 'coder')]), 3)
    check = manager.tasks['a'].checks[0]
    assert check['timed_out'] is True and check['exit_code'] != 0
