"""评测的用户路径：真实文件验收、模式对比、错误完成、预算、可信任务边界。"""

import asyncio
import json
from dataclasses import asdict

import httpx
import pytest
from typer.testing import CliRunner

from mini_nanobot.cli import app
from mini_nanobot.evaluation import (
    aggregate,
    evaluate,
    load_cases,
    make_plan,
    run_trial,
    self_check,
    trace_metrics,
)
from mini_nanobot.provider import ModelProvider


@pytest.fixture
def case():
    return next(case for case in load_cases() if case.id == 'clamp_and_slug')


async def fake_provider(settings, case, *, edit=True, wait=False, bad_review=False, repairable_review=False, fail_first=False, plan=None):
    """用协议替身代替网络，文件工具、角色调度、模型解析和外部验收仍真实执行。"""
    count = 0
    async def respond(request):
        nonlocal count
        count += 1
        if wait:
            await asyncio.Event().wait()
        payload = json.loads(request.content)
        assert all(not any(key.startswith('_') for key in message) for message in payload['messages'])
        system, last = payload['messages'][0]['content'], payload['messages'][-1]
        if plan and count == 1:
            message = {'tool_calls': [{'index': 0, 'id': 'delegate', 'function': {'name': 'subagent',
                       'arguments': json.dumps({'action': 'plan', 'tasks': plan, 'wait': False})}}]}
            finish = 'tool_calls'
        elif not edit or (fail_first and count == 1) or last['role'] == 'tool' or '你是探索 agent' in system or '你是审查 agent' in system or (plan and '当前角色：' not in system):
            message = {'content': json.dumps({'outcome': 'completed', 'summary': '已按要求完成', 'unresolved': []})}
            if bad_review and '你是审查 agent' in system and not (repairable_review and not payload.get('tools')):
                message = {'content': '已检查，没有问题。'}
            finish = 'stop'
        else:
            selected = case.reference_files
            if '你是实现 agent' in system:
                owned = last['content'].split('仅修改：', 1)[1].split('。', 1)[0].split(', ')
                selected = {path: case.reference_files[path] for path in owned}
            message = {'tool_calls': [
                {'index': index, 'id': f'write-{index}', 'function': {'name': 'write_file',
                 'arguments': json.dumps({'path': path, 'content': content})}}
                for index, (path, content) in enumerate(selected.items())]}
            finish = 'tool_calls'
        packets = [{'choices': [{'delta': message}]}, {'choices': [{'delta': {}, 'finish_reason': finish}]},
                   {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}]
        return httpx.Response(200, text=''.join('data: ' + json.dumps(packet) + '\n\n' for packet in packets) + 'data: [DONE]\n\n')
    provider = ModelProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return provider


async def test_all_builtin_cases_reject_broken_code_and_accept_reference(tmp_path, settings):
    report = await self_check(load_cases(), tmp_path / 'suite', settings)
    assert report['passed']
    assert len(report['cases']) == 16
    assert all(not row['original']['passed'] and row['reference']['passed'] for row in report['cases'])


@pytest.mark.parametrize('mode', ['single', 'serial', 'parallel', 'auto', 'auto_legacy'])
async def test_evaluation_writes_real_files_validates_and_exports_trace(tmp_path, settings, case, mode):
    provider = await fake_provider(settings, case)
    try:
        result = await run_trial(case, mode, 1, settings, tmp_path, 20, 10, provider)
    finally:
        await provider.close()
    assert result['success'] and result['acceptance']['passed']
    directory = tmp_path / result['artifacts']
    assert not (directory / 'workspace' / 'validation.py').exists()
    events = [json.loads(line) for line in (directory / 'trace.jsonl').read_text().splitlines()]
    model_events = [event for event in events if event['type'] == 'model']
    assert model_events and all(event['run_id'] == result['run_id'] for event in model_events)
    assert all(event['timestamp'] and event['span_id'] and event['task_id'] for event in model_events)
    assert result['metrics']['total_tokens'] == 15 * result['metrics']['http_requests']
    if mode in {'serial', 'parallel'}:
        assert {event['role'] for event in model_events} == {'explorer', 'coder', 'reviewer'}
        assert all(event['parent_task_id'] == 'main' for event in model_events)
        changes = [event for event in events if event['type'] == 'tool' and event.get('evidence')]
        assert {event['task_id'] for event in changes} == {'fix_numbers', 'fix_text'}
    else:
        assert result['metrics']['subtasks_created'] == 0
        assert result['metrics']['subagent_tool_calls'] == 0
    assert settings.api_key not in (directory / 'trace.jsonl').read_text()
    assert settings.api_key not in (directory / 'result.json').read_text()


async def test_claimed_completion_cannot_replace_external_acceptance(tmp_path, settings, case):
    provider = await fake_provider(settings, case, edit=False)
    try:
        result = await run_trial(case, 'single', 1, settings, tmp_path, 10, 10, provider)
    finally:
        await provider.close()
    assert result['reported_outcome'] == 'completed'
    assert result['false_completion'] and not result['success']
    assert result['acceptance']['exit_code'] != 0
    summary = aggregate([result])[0]
    assert summary['false_completion_rate'] == 1 and summary['success_rate'] == 0


@pytest.mark.parametrize('mode', ['single', 'parallel', 'auto'])
async def test_budget_is_shared_across_roles_and_saved_as_failure(tmp_path, settings, case, mode):
    provider = await fake_provider(settings, case)
    try:
        result = await run_trial(case, mode, 1, settings, tmp_path, 1, 10, provider)
    finally:
        await provider.close()
    assert result['execution_status'] == 'budget_exceeded'
    assert result['metrics']['http_requests'] <= 1
    assert not result['success']
    assert aggregate([result])[0]['execution_errors'] == 1


@pytest.mark.parametrize('isolated', [False, True])
@pytest.mark.parametrize('edit', [False, True])
async def test_auto_waits_for_model_chosen_tasks_and_keeps_actual_results(tmp_path, settings, case, isolated, edit):
    provider = await fake_provider(settings, case, plan=make_plan(case, 'parallel'), edit=edit)
    try:
        result = await run_trial(case, 'auto', 1, settings, tmp_path, 20, 10, provider, isolated=isolated)
    finally:
        await provider.close()
    assert result['success'] is edit
    assert result['acceptance']['passed'] is edit
    assert result['metrics']['subtasks_created'] == 4
    assert result['metrics']['subagent_tool_calls'] == 1
    assert result['metrics']['model_calls'] > 4
    assert aggregate([result])[0]['delegation_rate'] == 1
    directory = tmp_path / result['artifacts']
    assert not (directory / 'plan.json').exists()
    handoffs = json.loads((directory / 'handoffs.json').read_text())
    assert len(handoffs) == 4
    if isolated and edit:
        assert result['integration']['status'] == 'integrated'


async def test_total_deadline_cancels_model_and_saves_trace(tmp_path, settings, case):
    provider = await fake_provider(settings, case, wait=True)
    try:
        result = await run_trial(case, 'parallel', 1, settings, tmp_path, 10, 1, provider)
    finally:
        await provider.close()
    assert result['execution_status'] == 'timeout' and not result['success']
    events = [json.loads(line) for line in (tmp_path / result['artifacts'] / 'trace.jsonl').read_text().splitlines()]
    assert any(event['type'] == 'model' and event['state'] == 'cancelled' for event in events)
    assert not any(task.get_coro().__qualname__.startswith('SubagentManager.run') for task in asyncio.all_tasks())


def test_serial_and_parallel_have_same_work_and_different_dependencies(case):
    serial, parallel = make_plan(case, 'serial'), make_plan(case, 'parallel')
    assert [{key: value for key, value in item.items() if key != 'depends_on'} for item in serial] == [
        {key: value for key, value in item.items() if key != 'depends_on'} for item in parallel]
    assert serial[2]['depends_on'] == [serial[1]['id']]
    assert parallel[2]['depends_on'] == ['explore']
    assert serial[-1]['depends_on'] == parallel[-1]['depends_on']


def test_unknown_or_partial_usage_is_not_reported_as_zero():
    events = [
        {'type': 'model', 'state': 'running', 'request_id': 'one'},
        {'type': 'model', 'state': 'done', 'request_id': 'one', 'usage': {'total_tokens': 15}},
        {'type': 'model', 'state': 'running', 'request_id': 'two'},
        {'type': 'model', 'state': 'error', 'request_id': 'two', 'usage': None},
    ]
    metrics = trace_metrics(events, 1)
    assert metrics['http_requests'] == 2 and metrics['model_calls'] == 1
    assert metrics['total_tokens'] is None and metrics['known_total_tokens'] == 15
    assert metrics['usage_coverage'] == 0.5


@pytest.mark.parametrize('path', ['../outside.py', '/tmp/outside.py', '.env.py', '.git/config.py'])
def test_custom_suite_cannot_write_outside_workspace(tmp_path, case, path):
    data = asdict(case)
    data['files'] = data['reference_files'] = {path: 'pass\n'}
    suite = tmp_path / 'suite.json'
    suite.write_text(json.dumps({'version': 1, 'cases': [data]}))
    with pytest.raises((ValueError, PermissionError)):
        load_cases(suite)
    assert not (tmp_path / 'outside.py').exists()


def test_cli_self_check_runs_without_key_and_does_not_overwrite_results(tmp_path):
    runner = CliRunner()
    output = tmp_path / 'results'
    args = ['eval', '--case', 'arithmetic', '--self-check', '--output', str(output)]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    saved = (output / 'self_check.json').read_text()
    second = runner.invoke(app, args)
    assert second.exit_code != 0
    assert (output / 'self_check.json').read_text() == saved


def test_cli_output_limit_reaches_evaluation_without_changing_config(tmp_path, settings, monkeypatch):
    limits = []

    async def capture(cases, modes, repeats, config, output, *args, **kwargs):
        limits.append(config.max_output_tokens)
        return {'summary': []}

    monkeypatch.setattr('mini_nanobot.cli.load_settings', lambda *args: settings)
    monkeypatch.setattr('mini_nanobot.evaluation.evaluate', capture)
    result = CliRunner().invoke(app, ['eval', '--case', 'arithmetic', '--mode', 'auto',
        '--max-output-tokens', '8192', '--output', str(tmp_path / 'results')])
    assert result.exit_code == 0, result.output
    assert limits == [8192]
    assert not (settings.home / 'config.json').exists()


async def test_complete_comparison_exports_reproducible_report(tmp_path, settings, case, monkeypatch):
    providers = [await fake_provider(settings, case) for _ in range(5)]
    monkeypatch.setattr('mini_nanobot.evaluation.ModelProvider', lambda config: providers.pop())
    output = tmp_path / 'comparison'
    report = await evaluate([case], ['single', 'serial', 'parallel', 'auto', 'auto_legacy'], 1, settings, output, progress=lambda message: None)
    assert len(report['results']) == 5 and all(row['success'] for row in report['results'])
    assert {row['mode'] for row in report['summary']} == {'single', 'serial', 'parallel', 'auto', 'auto_legacy'}
    assert report['summary_by_category']['independent'] == report['summary']
    assert report['engine_hash'] and report['cases'][0]['hash'] == case.fingerprint
    assert (output / 'manifest.json').is_file() and (output / 'report.md').is_file()
    assert settings.api_key not in (output / 'report.json').read_text()
    assert json.loads((output / 'report.json').read_text())['summary'] == report['summary']
    prompts = json.loads((output / 'delegation_prompts.json').read_text())
    assert set(prompts) == set(report['delegation_prompt_hashes']) == {'current', 'legacy'}


async def test_legacy_prompt_replay_does_not_change_live_tool_description(tmp_path, settings, case):
    from mini_nanobot.evaluation import LEGACY_SUBAGENT_DESCRIPTION
    from mini_nanobot.tools.registry import SubagentTool

    current_description = SubagentTool.description
    for mode in ['auto_legacy', 'auto']:
        descriptions = []
        provider = await fake_provider(settings, case)
        original_send = provider.client.send

        async def capture(request, **kwargs):
            payload = json.loads(request.content)
            descriptions.extend(tool['function']['description'] for tool in payload.get('tools', [])
                                if tool['function']['name'] == 'subagent')
            return await original_send(request, **kwargs)

        provider.client.send = capture
        try:
            result = await run_trial(case, mode, 1, settings, tmp_path, 20, 10, provider)
        finally:
            await provider.close()
        assert result['success']
        expected = LEGACY_SUBAGENT_DESCRIPTION if mode == 'auto_legacy' else current_description
        assert descriptions and all(value == expected for value in descriptions)
        assert SubagentTool.description == current_description


async def test_routing_suite_rejects_defects_and_accepts_reference(tmp_path, settings):
    from importlib.resources import files

    suite = files('mini_nanobot').joinpath('eval_suites/routing_checks.json')
    cases = load_cases(suite)
    report = await self_check(cases, tmp_path / 'routing_suite', settings)
    assert report['passed'] and len(report['cases']) == 2


async def test_malformed_review_is_execution_error_even_if_code_passes(tmp_path, settings, case):
    provider = await fake_provider(settings, case, bad_review=True)
    try:
        result = await run_trial(case, 'serial', 1, settings, tmp_path, 20, 10, provider)
    finally:
        await provider.close()
    assert result['acceptance']['passed']
    assert result['execution_status'] == 'error' and not result['success']
    assert '交接格式无效' in result['error']


async def test_http_retries_consume_shared_request_budget(tmp_path, settings, case, monkeypatch):
    calls = []
    async def no_sleep(seconds):
        pass
    monkeypatch.setattr('mini_nanobot.provider.asyncio.sleep', no_sleep)
    def respond(request):
        calls.append(request)
        return httpx.Response(503)
    provider = ModelProvider(settings)
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        result = await run_trial(case, 'single', 1, settings, tmp_path, 1, 10, provider)
    finally:
        await provider.close()
    assert len(calls) == 1
    assert result['execution_status'] == 'budget_exceeded'
    assert result['metrics']['http_requests'] == 1
    assert result['metrics']['total_tokens'] is None


async def test_failed_code_gets_limited_repair_and_preserves_first_failure(tmp_path, settings, case):
    provider = await fake_provider(settings, case, fail_first=True)
    try:
        result = await run_trial(case, 'single', 1, settings, tmp_path, 10, 10, provider, max_repairs=1)
    finally:
        await provider.close()
    assert result['success'] and not result['first_pass_success']
    assert result['repairs_used'] == 1
    assert [item['validation']['status'] for item in result['attempts']] == ['failed', 'passed']
    assert result['metrics']['model_calls'] == 3
    events = [json.loads(line) for line in (tmp_path / result['artifacts'] / 'trace.jsonl').read_text().splitlines()]
    models = [event for event in events if event['type'] == 'model' and event['state'] == 'done']
    assert {event['workflow_attempt'] for event in models} == {1, 2}
    assert {event['attempt'] for event in models} == {1}


async def test_review_format_correction_is_measured_in_same_budget(tmp_path, settings, case):
    provider = await fake_provider(settings, case, bad_review=True, repairable_review=True)
    try:
        result = await run_trial(case, 'parallel', 1, settings, tmp_path, 10, 10, provider, report_repairs=1)
    finally:
        await provider.close()
    assert result['success'] and result['repairs_used'] == 0
    events = [json.loads(line) for line in (tmp_path / result['artifacts'] / 'trace.jsonl').read_text().splitlines()]
    assert any(event['type'] == 'model' and event.get('purpose') == 'report_repair' for event in events)
    assert result['metrics']['total_tokens'] == result['metrics']['http_requests'] * 15
