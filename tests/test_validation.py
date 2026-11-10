"""可信验收与有限修复的真实用户路径、权限和版本边界。"""

import asyncio
import json
import shlex
import sqlite3
import sys
from dataclasses import replace

import pytest

from mini_nanobot.agent import AgentService
from mini_nanobot.config import ValidationRule, load_settings
from mini_nanobot.provider import BudgetedProvider
from mini_nanobot.runner import AgentRunner
from mini_nanobot.session import SessionStore
from mini_nanobot.subagents import SubagentManager, TaskQuota
from mini_nanobot.tools.registry import ToolRegistry, build_tools
from mini_nanobot.validation import ValidationRunner


def command(script):
    return shlex.join([sys.executable, '-B', '-c', script])


def completion(summary='完成', outcome='completed'):
    return {'role': 'assistant', 'content': json.dumps({'outcome': outcome, 'summary': summary, 'unresolved': []})}


def spec(identity, dependencies=(), validation=()):
    return {'id': identity, 'role': 'coder', 'task': identity, 'depends_on': list(dependencies),
            'acceptance': ['满足加法需求'], 'validation': list(validation)}


@pytest.fixture
def checked_settings(settings):
    (settings.workspace / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
    settings.validation_rules = [ValidationRule('tests', command('from calc import add; assert add(2,3)==5; print("实际通过")'), ['calc.py'])]
    return settings


async def test_chat_repairs_failed_acceptance_and_persists_version(checked_settings, model_factory, call):
    settings = checked_settings
    model = model_factory([
        {'role': 'assistant', 'content': '已经完成。'},
        call('edit_file', path='calc.py', old='a - b', new='a + b'),
        {'role': 'assistant', 'content': '根据失败反馈修复了加法。'},
    ])
    service = AgentService(settings, model)
    try:
        item = service.create_session(str(settings.workspace))
        await service.start(item['id'], '修复 add')
        saved = service.snapshot(item['id'])
        assert saved['status'] == 'idle'
        assert saved['validation']['status'] == 'passed' and saved['validation']['attempt'] == 2
        assert saved['validation']['checks'][0]['exit_code'] == 0
        validations = [event for event in saved['events'] if event['type'] == 'validation' and event['state'] != 'running']
        assert [event['state'] for event in validations] == ['failed', 'passed']
        assert '后端验收未通过' in model.requests[1][-1]['content']
        assert '当前用户原始任务' in model.requests[1][-1]['content'] and '修复 add' in model.requests[1][-1]['content']
        assert settings.validation_rules[0].command not in model.requests[1][-1]['content']
        # 代码再次改变时，原退出码保留，但 API 快照不再声称当前版本通过。
        (settings.workspace / 'calc.py').write_text('def add(a,b): return 0\n')
        stale = service.snapshot(item['id'])['validation']
        assert stale['status'] == 'stale' and stale['checks'][0]['exit_code'] == 0
    finally:
        await service.close()
    store = SessionStore(settings.home)
    try:
        assert store.get(item['id'])['validation']['status'] == 'stale'
    finally:
        store.close()


async def test_repair_limit_keeps_failure_instead_of_claiming_done(checked_settings, model_factory):
    settings = checked_settings
    settings.max_repairs = 1
    model = model_factory([{'role': 'assistant', 'content': '已完成'}] * 2)
    service = AgentService(settings, model)
    try:
        item = service.create_session(str(settings.workspace))
        await service.start(item['id'], '修复 add')
        saved = service.snapshot(item['id'])
        assert saved['status'] == 'needs_followup'
        assert saved['validation']['status'] == 'failed' and saved['validation']['attempt'] == 2
        assert len(model.requests) == 2
        assert '后端验收未通过' in saved['messages'][-1]['content']
    finally:
        await service.close()


async def test_repairs_share_request_budget_and_do_not_replay_tools(checked_settings, model_factory):
    settings = checked_settings
    settings.max_model_calls = 1
    model = model_factory([{'role': 'assistant', 'content': '已完成'}])
    service = AgentService(settings, model)
    try:
        item = service.create_session(str(settings.workspace))
        await service.start(item['id'], '修复 add')
        saved = service.snapshot(item['id'])
        assert saved['status'] == 'error' and len(model.requests) == 1
        assert saved['validation']['status'] == 'not_run'
        assert saved['validation']['attempt'] == 2
        assert 'a - b' in (settings.workspace / 'calc.py').read_text()
    finally:
        await service.close()


async def test_cancel_during_backend_validation_is_never_passed(checked_settings, model_factory):
    settings = checked_settings
    settings.validation_rules = [ValidationRule('slow', 'sleep 30', ['calc.py'])]
    entered = asyncio.Event()
    async def emit(event):
        if event['type'] == 'validation' and event['state'] == 'running':
            entered.set()
    service = AgentService(settings, model_factory([{'role': 'assistant', 'content': '已完成'}]))
    try:
        item = service.create_session(str(settings.workspace))
        service.start(item['id'], '修复 add', emit)
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(service.stop(item['id']), 2)
        saved = service.snapshot(item['id'])
        assert saved['status'] == 'cancelled'
        assert saved['validation']['status'] != 'passed'
    finally:
        await service.close()


@pytest.mark.parametrize('invalid_content', ['不是 JSON', '', None])
async def test_report_correction_has_no_tools_and_does_not_repeat_work(settings, model_factory, invalid_content):
    async def corrected(messages, tools, emit):
        assert not tools
        return completion('保留已有事实')
    model = model_factory([{'role': 'assistant', 'content': invalid_content}, corrected])
    budget = BudgetedProvider(model, 2)
    manager = SubagentManager(AgentRunner(budget, settings), '测试', lambda role: ToolRegistry([]),
                             lambda event: asyncio.sleep(0), asyncio.Semaphore(1), report_repairs=1)
    try:
        await manager.dispatch('plan', tasks=[spec('a')])
        assert manager.tasks['a'].status == 'done'
        assert budget.calls == 2 and len(model.requests) == 2
    finally:
        await manager.close()


async def test_report_correction_rejects_tool_calls_without_writing(settings, model_factory, call):
    store = SessionStore(settings.home)
    lock = asyncio.Lock()
    model = model_factory([{'role': 'assistant', 'content': '不是 JSON'}, call('write_file', path='unexpected.py', content='x')])
    manager = SubagentManager(AgentRunner(model, settings), '测试',
        lambda role: build_tools(settings.workspace, settings, store, lock, role),
        lambda event: asyncio.sleep(0), asyncio.Semaphore(1), report_repairs=1)
    try:
        await manager.dispatch('plan', tasks=[spec('a')])
        assert manager.tasks['a'].status == 'error'
        assert not (settings.workspace / 'unexpected.py').exists()
    finally:
        await manager.close()
        store.close()


async def test_task_claim_cannot_bypass_validation_or_start_descendants(checked_settings, model_factory):
    settings = checked_settings
    store = SessionStore(settings.home)
    lock = asyncio.Lock()
    model = model_factory([completion('我已测试通过')])
    manager = SubagentManager(AgentRunner(model, settings), '测试',
        lambda role: build_tools(settings.workspace, settings, store, lock, role),
        lambda event: asyncio.sleep(0), asyncio.Semaphore(1), validator=ValidationRunner(settings.workspace, settings))
    try:
        await manager.dispatch('plan', tasks=[spec('a', validation=['tests']), spec('b', dependencies=['a'])])
        assert manager.tasks['a'].reported_outcome == 'completed'
        assert manager.tasks['a'].validation['status'] == 'failed'
        assert manager.tasks['a'].status == 'needs_followup' and manager.tasks['b'].status == 'blocked'
        assert len(model.requests) == 1
        with pytest.raises(ValueError, match='可信后端配置'):
            await manager.dispatch('plan', tasks=[spec('c'), spec('d', validation=['invented'])])
        assert 'c' not in manager.tasks and 'd' not in manager.tasks
    finally:
        await manager.close()
        store.close()


async def test_stale_task_handoff_blocks_new_dependents(checked_settings, model_factory):
    settings = checked_settings
    path = settings.workspace / 'calc.py'
    path.write_text('def add(a,b): return a+b\n')
    store = SessionStore(settings.home)
    model = model_factory([completion()])
    manager = SubagentManager(AgentRunner(model, settings), '测试',
        lambda role: build_tools(settings.workspace, settings, store, asyncio.Lock(), role),
        lambda event: asyncio.sleep(0), asyncio.Semaphore(1), validator=ValidationRunner(settings.workspace, settings))
    try:
        await manager.dispatch('plan', tasks=[spec('a', validation=['tests'])])
        assert manager.tasks['a'].status == 'done'
        path.write_text('def add(a,b): return a-b\n')
        await manager.dispatch('plan', tasks=[spec('b', dependencies=['a'])])
        assert manager.tasks['a'].validation['status'] == 'stale'
        assert manager.tasks['b'].status == 'blocked' and len(model.requests) == 1
    finally:
        await manager.close()
        store.close()


@pytest.mark.parametrize('path', ['../outside.py', '.env', 'escape.py'])
async def test_invalid_artifact_scope_fails_before_command(settings, tmp_path, path):
    outside = tmp_path / 'outside.py'
    outside.write_text('secret')
    (settings.workspace / 'escape.py').symlink_to(outside)
    rule = ValidationRule('check', 'touch should-not-run', [path])
    result = await ValidationRunner(settings.workspace, settings, [rule]).run()
    assert result['status'] == 'error' and not result['checks']
    assert not (settings.workspace / 'should-not-run').exists()


async def test_validator_mutation_and_policy_change_invalidate_success(checked_settings):
    settings = checked_settings
    path = settings.workspace / 'calc.py'
    rule = ValidationRule('mutates', command('from pathlib import Path; Path("calc.py").write_text("x=1\\n")'), ['calc.py'])
    result = await ValidationRunner(settings.workspace, settings, [rule]).run()
    assert result['checks'][0]['exit_code'] == 0 and result['status'] == 'stale'
    path.write_text('def add(a,b): return a+b\n')
    runner = ValidationRunner(settings.workspace, settings)
    passed = await runner.run()
    assert passed['status'] == 'passed'
    changed = ValidationRunner(settings.workspace, settings, [replace(settings.validation_rules[0], command='exit 1')])
    assert changed.refresh(passed)['status'] == 'stale'


def test_session_schema_upgrade_preserves_old_data(tmp_path):
    home = tmp_path / 'profile'
    home.mkdir()
    connection = sqlite3.connect(home / 'mini_sessions.sqlite3')
    connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY,title TEXT,workspace TEXT,messages TEXT,summary TEXT,status TEXT,updated REAL)")
    connection.execute("INSERT INTO sessions VALUES ('old','旧任务','/tmp','[]','保留摘要','idle',1)")
    connection.commit()
    connection.close()
    store = SessionStore(home)
    try:
        assert store.get('old')['summary'] == '保留摘要'
        assert store.get('old')['validation'] == {}
        store.save_validation('old', {'status': 'failed', 'checks': []})
        assert store.get('old')['validation']['status'] == 'failed'
    finally:
        store.close()


@pytest.mark.parametrize('policy', [
    {'commands': [{'id': 'tests', 'command': 'exit 0', 'untrusted': True}]},
    {'commands': [{'id': 'tests', 'command': 'exit 0'}, {'id': 'tests', 'command': 'exit 1'}]},
    {'maxRepairAttempts': 99},
])
def test_invalid_trusted_policy_is_rejected_at_load(tmp_path, policy):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'validation': policy}))
    with pytest.raises(ValueError):
        load_settings(tmp_path / 'profile', config)


async def test_new_attempt_cannot_reset_request_wide_subtask_quota(settings, model_factory):
    quota = TaskQuota(limit=1)
    model = model_factory([completion()])
    managers = [SubagentManager(AgentRunner(model, settings), '测试', lambda role: ToolRegistry([]),
                               lambda event: asyncio.sleep(0), asyncio.Semaphore(1), quota=quota) for _ in range(2)]
    try:
        await managers[0].dispatch('plan', tasks=[spec('first')])
        with pytest.raises(ValueError, match='全部修复轮次'):
            await managers[1].dispatch('plan', tasks=[spec('second')])
        assert not managers[1].tasks and len(model.requests) == 1
    finally:
        for manager in managers:
            await manager.close()


@pytest.mark.parametrize('role', ['explorer', 'reviewer'])
async def test_read_only_role_cannot_request_backend_execution(checked_settings, model_factory, role):
    settings = checked_settings
    model = model_factory([])
    manager = SubagentManager(AgentRunner(model, settings), '测试', lambda role: ToolRegistry([]),
        lambda event: asyncio.sleep(0), asyncio.Semaphore(1), validator=ValidationRunner(settings.workspace, settings))
    task = {**spec('reader', validation=['tests']), 'role': role}
    try:
        with pytest.raises(ValueError, match='只有实现任务'):
            await manager.dispatch('plan', tasks=[task])
        assert not model.requests and not manager.tasks
    finally:
        await manager.close()


async def test_invalid_report_after_correction_limit_stops(settings, model_factory):
    model = model_factory([{'role': 'assistant', 'content': '无效 JSON'}] * 2)
    manager = SubagentManager(AgentRunner(model, settings), '测试', lambda role: ToolRegistry([]),
        lambda event: asyncio.sleep(0), asyncio.Semaphore(1), report_repairs=1)
    try:
        await manager.dispatch('plan', tasks=[spec('a'), spec('b', dependencies=['a'])])
        assert manager.tasks['a'].status == 'error' and manager.tasks['b'].status == 'blocked'
        assert len(model.requests) == 2
    finally:
        await manager.close()


async def test_empty_corrections_stay_bounded_and_do_not_send_empty_assistant(settings, model_factory):
    async def corrected(messages, tools, emit):
        assert not tools
        assert all(row.get('content') or row.get('tool_calls') for row in messages if row['role'] == 'assistant')
        return completion()
    model = model_factory([{'role': 'assistant', 'content': None}, {'role': 'assistant', 'content': None}, corrected])
    manager = SubagentManager(AgentRunner(model, settings), '测试', lambda role: ToolRegistry([]),
        lambda event: asyncio.sleep(0), asyncio.Semaphore(1), report_repairs=2)
    try:
        await manager.dispatch('plan', tasks=[spec('a')])
        assert manager.tasks['a'].status == 'done' and len(model.requests) == 3
    finally:
        await manager.close()


async def test_request_deadline_cleans_up_and_session_can_continue(settings, model_factory):
    settings.run_timeout = 1
    async def wait_forever(messages, tools, emit):
        await asyncio.Event().wait()
    model = model_factory([wait_forever, {'role': 'assistant', 'content': '继续完成'}])
    service = AgentService(settings, model)
    try:
        item = service.create_session(str(settings.workspace))
        await asyncio.wait_for(service.start(item['id'], '执行任务'), 2)
        saved = service.snapshot(item['id'])
        assert saved['status'] == 'error' and item['id'] not in service.active
        assert saved['validation']['status'] == 'not_run'
        await service.start(item['id'], '继续')
        assert service.snapshot(item['id'])['status'] == 'idle'
    finally:
        await service.close()
