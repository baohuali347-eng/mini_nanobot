"""主循环和多 agent 的端到端执行契约。"""
import asyncio

from mini_nanobot.agent import AgentService
from mini_nanobot.context import compact, repair_history


async def test_explore_implement_review_round_trip(settings, model_factory, call):
    source = settings.workspace / 'calculator.py'
    source.write_text('def add(a, b):\n    return a - b\n')
    model = model_factory([
        call('subagent', action='create', role='explorer', task='读取 calculator.py', wait=True),
        call('read_file', path='calculator.py'),
        {'role': 'assistant', 'content': '发现减法错误。'},
        call('subagent', action='create', role='coder', task='改为加法', wait=True),
        call('edit_file', path='calculator.py', old='return a - b', new='return a + b'),
        {'role': 'assistant', 'content': '实现完成。'},
        call('subagent', action='create', role='reviewer', task='审查实现', wait=True),
        call('read_file', path='calculator.py'),
        {'role': 'assistant', 'content': '独立审查通过。'},
        {'role': 'assistant', 'content': '任务完成。'},
    ])
    service = AgentService(settings, model)
    try:
        session = service.create_session(str(settings.workspace))
        await service.start(session['id'], '完成探索、实现、审查。')
        saved = service.snapshot(session['id'])
        assert saved['status'] == 'idle'
        assert saved['messages'][-1]['content'] == '任务完成。'
        tasks = [event for event in saved['events'] if event['type'] == 'task' and event['state'] == 'done']
        assert {event['role'] for event in tasks} == {'explorer', 'coder', 'reviewer'}
        namespace = {}
        exec(source.read_text(), namespace)
        assert namespace['add'](2, 3) == 5
    finally:
        await service.close()


async def test_background_results_reach_parent_before_final_answer(settings, model_factory, call):
    model = model_factory([
        call('subagent', action='create', role='explorer', task='检查目录', wait=False),
        {'role': 'assistant', 'content': '暂时结论。'},
        {'role': 'assistant', 'content': '探索完成。'},
        {'role': 'assistant', 'content': '依据子任务结果给出最终结论。'},
    ])
    service = AgentService(settings, model)
    try:
        item = service.create_session(str(settings.workspace))
        await service.start(item['id'], '安排一个后台探索任务。')
        assert service.store.get(item['id'])['status'] == 'idle'
        assert service.snapshot(item['id'])['messages'][-1]['content'] == '依据子任务结果给出最终结论。'
        assert any('子任务结果通知' in str(row) for row in model.requests[-1])
    finally:
        await service.close()


async def test_immediate_and_running_cancellation_leave_session_usable(settings, model_factory):
    entered = asyncio.Event()
    async def waiting(*args):
        entered.set()
        await asyncio.Event().wait()
    model = model_factory([waiting, {'role': 'assistant', 'content': '恢复正常。'}])
    service = AgentService(settings, model)
    try:
        item = service.create_session(str(settings.workspace))
        service.start(item['id'], '尚未开始')
        await service.stop(item['id'])
        assert item['id'] not in service.active
        service.start(item['id'], '开始后停止')
        await entered.wait()
        await service.stop(item['id'])
        assert service.store.get(item['id'])['status'] == 'cancelled'
        await service.start(item['id'], '继续')
        assert service.store.get(item['id'])['status'] == 'idle'
    finally:
        await service.close()


async def test_tool_failure_is_returned_to_model(settings, model_factory, call):
    model = model_factory([
        call('read_file', path='../private.txt'),
        {'role': 'assistant', 'content': '当前路径超出权限，请选择项目内文件。'},
    ])
    service = AgentService(settings, model)
    try:
        item = service.create_session(str(settings.workspace))
        await service.start(item['id'], '读取文件')
        assert '工具错误' in model.requests[-1][-1]['content']
        assert service.store.get(item['id'])['status'] == 'idle'
    finally:
        await service.close()


async def test_context_compaction_keeps_task_and_complete_tool_groups(model_factory, call):
    history = [{'role': 'user', 'content': '当前任务：修复 add'}]
    for index in range(5):
        action = call('read_file', path='app.py')
        action['tool_calls'][0]['id'] += str(index)
        history.extend([action, {'role': 'tool', 'tool_call_id': action['tool_calls'][0]['id'], 'content': '代码' * 600}])
    model = model_factory([{'role': 'assistant', 'content': '已经定位 add 的减法错误，尚未修改。'}])
    remaining, summary = await compact(model, history, '', 500)
    assert '减法错误' in summary
    assert any(row.get('content') == '当前任务：修复 add' for row in remaining)
    assert repair_history(remaining) == remaining
    assert len(remaining) < len(history)


def test_interrupted_tool_calls_are_not_replayed(call):
    history = [{'role': 'user', 'content': '写入文件'}, call('write_file', path='app.py', content='x')]
    fixed = repair_history(history)
    assert not any(row.get('tool_calls') for row in fixed)
    assert '避免重复执行' in fixed[-1]['content']


async def test_thinking_only_truncation_stops_without_invalid_continuation(settings, model_factory):
    model = model_factory([{'role': 'assistant', 'content': None, 'reasoning_content': '未完成的内部推理',
                            '_finish_reason': 'length'}])
    service = AgentService(settings, model)
    try:
        item = service.create_session(str(settings.workspace))
        await service.start(item['id'], '修复代码')
        saved = service.snapshot(item['id'])
        assert saved['status'] == 'error' and len(model.requests) == 1
        assert not any(message['role'] == 'assistant' and not message.get('content') for message in saved['messages'])
        assert any(event['type'] == 'error' and 'maxOutputTokens' in event['message'] for event in saved['events'])
    finally:
        await service.close()


async def test_truncated_tool_response_never_partially_writes_files(settings, model_factory, call):
    source = settings.workspace / 'app.py'
    source.write_text('original\n')
    action = call('write_file', path='app.py', content='replacement\n')
    action['_finish_reason'] = 'length'
    model = model_factory([action])
    service = AgentService(settings, model)
    try:
        item = service.create_session(str(settings.workspace))
        await service.start(item['id'], '修改文件')
        assert service.snapshot(item['id'])['status'] == 'error'
        assert source.read_text() == 'original\n'
        assert len(model.requests) == 1
    finally:
        await service.close()


async def test_main_agent_sees_remaining_shared_calls_before_each_round(settings, model_factory, call):
    settings.max_model_calls = 2
    (settings.workspace / 'app.py').write_text('code\n')
    model = model_factory([call('read_file', path='app.py'), {'role': 'assistant', 'content': '已读取代码。'}])
    service = AgentService(settings, model)
    try:
        item = service.create_session(str(settings.workspace))
        await service.start(item['id'], '读取 app.py')
        assert service.snapshot(item['id'])['status'] == 'idle'
        assert '额度剩余 2 次' in model.requests[0][0]['content']
        assert '额度剩余 1 次' in model.requests[1][0]['content']
    finally:
        await service.close()


def test_old_thinking_only_message_can_resume_with_completed_tool_results(call):
    action = call('read_file', path='app.py')
    tool = {'role': 'tool', 'tool_call_id': action['tool_calls'][0]['id'], 'content': '代码'}
    history = [{'role': 'user', 'content': '修复代码'}, action, tool,
               {'role': 'assistant', 'content': None, 'reasoning_content': '被截断的推理'},
               {'role': 'user', 'content': '继续完成'}]
    repaired = repair_history(history)
    assert repaired == history[:3] + history[4:]


async def test_background_subtasks_obey_shared_concurrency_limit(settings, model_factory, call):
    settings.max_subagents = 1
    active, peak = 0, 0
    async def child(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.02)
        active -= 1
        return {'role': 'assistant', 'content': '独立探索已完成。'}
    first = call('subagent', action='create', role='explorer', task='探索 A', wait=False)
    second = call('subagent', action='create', role='reviewer', task='检查 B', wait=False)
    second['tool_calls'][0]['id'] += '-second'
    first['tool_calls'].extend(second['tool_calls'])
    model = model_factory([
        first, {'role': 'assistant', 'content': '等待汇总。'}, child, child,
        {'role': 'assistant', 'content': '两个任务均已完成。'},
    ])
    service = AgentService(settings, model)
    try:
        item = service.create_session(str(settings.workspace))
        await service.start(item['id'], '创建两个后台任务并汇总。')
        assert service.store.get(item['id'])['status'] == 'idle'
        assert peak == 1
        completed = [event for event in service.store.events(item['id']) if event['type'] == 'task' and event['state'] == 'done']
        assert len(completed) == 2
    finally:
        await service.close()
