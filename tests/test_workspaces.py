"""独立副本、依赖产物、组合验收、冲突保护和发布回滚的用户路径。"""

import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from mini_nanobot.config import ValidationRule
from mini_nanobot.runner import AgentRunner
from mini_nanobot.session import SessionStore
from mini_nanobot.subagents import SubagentManager
from mini_nanobot.tools.registry import build_tools
from mini_nanobot.tools.shell import ExecuteCommand
from mini_nanobot.validation import ValidationRunner
from mini_nanobot.workspaces import WorkspaceBundle, capture, child_environment


def report():
    return {'role': 'assistant', 'content': '{"outcome":"completed","summary":"已完成分配任务","unresolved":[]}'}


def task(identity, paths, dependencies=(), role='coder'):
    return {'id': identity, 'role': role, 'task': identity, 'depends_on': list(dependencies),
            'write_paths': paths, 'acceptance': ['满足分配需求']}


def view(messages):
    return Path(messages[0]['content'].split('本子任务真实目录：', 1)[1].split('。', 1)[0])


@pytest.fixture
def isolated(settings, model_factory):
    managers = []
    stores = []
    def make(responses, script=None):
        model = model_factory(responses)
        lock = asyncio.Lock()
        bundle = WorkspaceBundle(settings.workspace, settings.home, lock)
        store = SessionStore(settings.home)
        stores.append(store)
        rules = [ValidationRule('combined', shlex.join([sys.executable, '-B', '-c', script]))] if script else []
        events = []
        async def emit(event):
            events.append(event)
        manager = SubagentManager(AgentRunner(model, settings), '测试', lambda role: None, emit, asyncio.Semaphore(2),
            bundle=bundle, workspace_tools=lambda role, root, paths: build_tools(root, settings, store, asyncio.Lock(), role,
                write_paths=paths, memory_workspace=settings.workspace),
            validator_factory=lambda root: ValidationRunner(root, settings, rules))
        managers.append(manager)
        return manager, bundle, events, model
    yield make
    for store in stores:
        store.close()


async def test_parallel_views_are_independent_and_combined_code_is_validated(settings, isolated, call):
    root = settings.workspace
    (root / 'a.py').write_text('value = 0\n')
    (root / 'b.py').write_text('value = 0\n')
    (root / 'notes.txt').write_text('用户未提交内容')
    ready = asyncio.Event()
    entered = 0
    roots = set()
    async def start(messages, tools, emit):
        nonlocal entered
        identity = messages[1]['content'].splitlines()[0]
        roots.add(view(messages))
        entered += 1
        if entered == 2:
            ready.set()
        await ready.wait()
        return call('write_file', path=identity + '.py', content=f'value = {1 if identity == "a" else 2}\n')
    async def finish(messages, tools, emit):
        identity = messages[1]['content'].splitlines()[0]
        other = 'b' if identity == 'a' else 'a'
        assert (view(messages) / (other + '.py')).read_text() == 'value = 0\n'
        assert (root / 'a.py').read_text() == (root / 'b.py').read_text() == 'value = 0\n'
        return report()
    manager, bundle, events, model = isolated([start, start, finish, finish], 'from a import value as a; from b import value as b; assert a+b==3; print("组合通过")')
    try:
        await asyncio.wait_for(manager.dispatch('plan', tasks=[task('a', ['a.py']), task('b', ['b.py'])]), 5)
        assert len(roots) == 2 and root not in roots
        assert manager.integration['status'] == 'integrated'
        assert manager.integration['validation']['status'] == 'passed'
        assert (root / 'a.py').read_text() == 'value = 1\n' and (root / 'b.py').read_text() == 'value = 2\n'
        assert (root / 'notes.txt').read_text() == '用户未提交内容'
        assert any(event['type'] == 'integration' and event['state'] == 'integrated' for event in events)
        assert len(model.requests) == 4
    finally:
        await manager.close()
    assert not list(bundle.directory.glob('view-*'))
    assert (bundle.directory / 'a.diff').exists() and Path(manager.integration['journal']).exists()


async def test_dependent_task_sees_predecessor_patch_before_publication(settings, isolated, call):
    root = settings.workspace
    (root / 'a.py').write_text('value = 0\n')
    async def second(messages, tools, emit):
        assert (view(messages) / 'a.py').read_text() == 'value = 1\n'
        assert (root / 'a.py').read_text() == 'value = 0\n'
        return call('write_file', path='a.py', content='value = 2\n')
    manager, _, _, _ = isolated([call('write_file', path='a.py', content='value = 1\n'), report(), second, report()])
    try:
        await manager.dispatch('plan', tasks=[task('a', ['a.py']), task('b', ['a.py'], ['a'])])
        assert manager.integration['status'] == 'integrated'
        assert (root / 'a.py').read_text() == 'value = 2\n'
    finally:
        await manager.close()


async def test_independent_edits_of_same_file_conflict_without_overwrite(settings, isolated, call):
    (settings.workspace / 'a.py').write_text('value = 0\n')
    gate = asyncio.Event()
    count = 0
    async def start(messages, tools, emit):
        nonlocal count
        identity = messages[1]['content'].splitlines()[0]
        count += 1
        if count == 2:
            gate.set()
        await gate.wait()
        return call('write_file', path='a.py', content=f'value = {1 if identity == "a" else 2}\n')
    manager, bundle, _, _ = isolated([start, start, report(), report()])
    try:
        await manager.dispatch('plan', tasks=[task('a', ['a.py']), task('b', ['a.py'])])
        assert manager.integration['status'] == 'conflict' and manager.integration['files'] == ['a.py']
        assert manager.integration['kind'] == 'overlap'
        assert all(item.status == 'needs_followup' for item in manager.tasks.values())
        assert (settings.workspace / 'a.py').read_text() == 'value = 0\n'
        assert (bundle.directory / 'a.diff').exists() and (bundle.directory / 'b.diff').exists()
    finally:
        await manager.close()


async def test_combined_failure_blocks_review_model_and_preserves_original(settings, isolated, call):
    for name in ['a.py', 'b.py']:
        (settings.workspace / name).write_text('value = 0\n')
    manager, _, _, model = isolated([
        call('write_file', path='a.py', content='value = 1\n'), report(),
        call('write_file', path='b.py', content='value = 2\n'), report(),
    ], 'from a import value as a; from b import value as b; assert a+b==99')
    try:
        await manager.dispatch('plan', tasks=[task('a', ['a.py']), task('b', ['b.py'], ['a']), task('review', ['.'], ['a', 'b'], 'reviewer')])
        assert len(model.requests) == 4
        assert manager.tasks['review'].status == 'needs_followup'
        assert manager.integration['status'] == 'validation_failed'
        assert (settings.workspace / 'a.py').read_text() == (settings.workspace / 'b.py').read_text() == 'value = 0\n'
    finally:
        await manager.close()


async def test_shell_change_outside_ownership_is_detected_from_filesystem(settings, isolated, call):
    (settings.workspace / 'other.py').write_text('original\n')
    command = shlex.join([sys.executable, '-c', 'from pathlib import Path; Path("other.py").write_text("changed\\n")'])
    manager, _, _, _ = isolated([call('exec', command=command), report()])
    try:
        await manager.dispatch('plan', tasks=[task('a', ['allowed.py'])])
        assert manager.tasks['a'].status == 'needs_followup'
        assert manager.tasks['a'].checks[0]['exit_code'] == 0
        assert manager.integration['status'] == 'conflict'
        assert (settings.workspace / 'other.py').read_text() == 'original\n'
    finally:
        await manager.close()


async def test_concurrent_user_edit_makes_publication_stale(settings, isolated, call):
    (settings.workspace / 'a.py').write_text('old\n')
    (settings.workspace / 'notes.txt').write_text('user old\n')
    async def edited(messages, tools, emit):
        (settings.workspace / 'notes.txt').write_text('user new\n')
        return call('write_file', path='a.py', content='candidate\n')
    manager, _, _, _ = isolated([edited, report()])
    try:
        await manager.dispatch('plan', tasks=[task('a', ['a.py'])])
        assert manager.integration['status'] == 'conflict'
        assert (settings.workspace / 'a.py').read_text() == 'old\n'
        assert (settings.workspace / 'notes.txt').read_text() == 'user new\n'
    finally:
        await manager.close()


@pytest.mark.parametrize('external_edit', [False, True])
def test_publication_failure_rolls_back_only_its_own_writes(settings, monkeypatch, external_edit):
    root = settings.workspace
    for name in ['a.py', 'b.py']:
        (root / name).write_text('old\n')
    bundle = WorkspaceBundle(root, settings.home, asyncio.Lock())
    candidate = bundle.fork('fix', set())
    for name in ['a.py', 'b.py']:
        (candidate / name).write_text('new\n')
    bundle.collect('fix', ['a.py', 'b.py'])
    _, tree = bundle.stage({'fix'})
    original = bundle._replace
    def fail_after_write(name, version):
        original(name, version)
        if name == 'b.py' and version.data == b'new\n':
            if external_edit:
                (root / 'a.py').write_text('external\n')
            raise OSError('模拟发布时断开')
    monkeypatch.setattr(bundle, '_replace', fail_after_write)
    with pytest.raises(OSError):
        bundle.publish(tree, {'fix'})
    assert (root / 'b.py').read_text() == 'old\n'
    assert (root / 'a.py').read_text() == ('external\n' if external_edit else 'old\n')
    journal = json.loads((bundle.directory / 'journal-0.json').read_text())
    assert journal['status'] == ('recovery_required' if external_edit else 'rolled_back')
    bundle.cleanup()


def test_git_dirty_snapshot_keeps_index_and_untracked_user_files(settings):
    root = settings.workspace
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], env={**child_environment(), 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'})
    git('init', '-q')
    (root / 'a.py').write_text('committed\n')
    (root / '.gitignore').write_text('.env\n')
    git('add', 'a.py', '.gitignore')
    git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'fixture')
    (root / 'a.py').write_text('dirty\n')
    (root / 'notes.txt').write_text('untracked user data')
    (root / '.env').write_text('secret-not-exported')
    index = (root / '.git/index').read_bytes()
    head = git('rev-parse', 'HEAD')
    bundle = WorkspaceBundle(root, settings.home, asyncio.Lock())
    candidate = bundle.fork('fix', set())
    assert (candidate / 'a.py').read_text() == 'dirty\n'
    assert (candidate / 'notes.txt').read_text() == 'untracked user data'
    assert not (candidate / '.env').exists() and not (candidate / '.git').exists()
    (candidate / 'a.py').write_text('fixed\n')
    bundle.collect('fix', ['a.py'])
    _, tree = bundle.stage({'fix'})
    assert bundle.publish(tree, {'fix'})['status'] == 'integrated'
    assert (root / '.git/index').read_bytes() == index and git('rev-parse', 'HEAD') == head
    assert (root / 'notes.txt').read_text() == 'untracked user data'
    bundle.cleanup()


async def test_cancel_archives_partial_patch_but_never_publishes(settings, isolated, call):
    (settings.workspace / 'a.py').write_text('old\n')
    entered = asyncio.Event()
    async def waiting(messages, tools, emit):
        entered.set()
        await asyncio.Event().wait()
    manager, bundle, _, _ = isolated([call('write_file', path='a.py', content='partial\n'), waiting])
    await manager.dispatch('plan', tasks=[task('a', ['a.py'])], wait=False)
    await asyncio.wait_for(entered.wait(), 2)
    await manager.close()
    assert manager.tasks['a'].status == 'cancelled'
    assert (settings.workspace / 'a.py').read_text() == 'old\n'
    assert (bundle.directory / 'a.diff').exists() and not list(bundle.directory.glob('view-*'))


def test_snapshot_refuses_symlink_and_keeps_original(settings, tmp_path):
    outside = tmp_path / 'outside.py'
    outside.write_text('private')
    (settings.workspace / 'escape.py').symlink_to(outside)
    with pytest.raises((ValueError, PermissionError)):
        capture(settings.workspace)
    assert outside.read_text() == 'private'


@pytest.mark.parametrize('allowed', [False, True])
def test_original_deletion_requires_trusted_backend_policy(settings, allowed):
    (settings.workspace / 'a.py').write_text('user data\n')
    bundle = WorkspaceBundle(settings.workspace, settings.home, asyncio.Lock(), allow_deletes=allowed)
    candidate = bundle.fork('fix', set())
    (candidate / 'a.py').unlink()
    if allowed:
        bundle.collect('fix', ['a.py'])
        _, tree = bundle.stage({'fix'})
        bundle.publish(tree, {'fix'})
        assert not (settings.workspace / 'a.py').exists()
    else:
        with pytest.raises(ValueError, match='未授权'):
            bundle.collect('fix', ['a.py'])
        assert (settings.workspace / 'a.py').read_text() == 'user data\n'
    bundle.cleanup()


async def test_owned_shell_writes_are_captured_even_without_file_tool_evidence(settings, isolated, call):
    (settings.workspace / 'a.py').write_text('old\n')
    command = shlex.join([sys.executable, '-c', 'from pathlib import Path; Path("a.py").write_text("new\\n")'])
    manager, _, _, _ = isolated([call('exec', command=command), report()])
    try:
        await manager.dispatch('plan', tasks=[task('a', ['a.py'])])
        assert manager.integration['status'] == 'integrated'
        assert manager.tasks['a'].files_changed == []
        assert manager.tasks['a'].artifact['files'][0]['path'] == 'a.py'
        assert (settings.workspace / 'a.py').read_text() == 'new\n'
    finally:
        await manager.close()


async def test_file_tool_rejects_unassigned_write_before_effect(settings):
    store = SessionStore(settings.home)
    try:
        tools = build_tools(settings.workspace, settings, store, asyncio.Lock(), 'coder', write_paths=['allowed.py'])
        result = await tools.execute('write_file', {'path': 'other.py', 'content': 'unexpected'})
        assert not result.ok and not (settings.workspace / 'other.py').exists()
    finally:
        store.close()


async def test_review_receives_real_combined_evidence_without_private_script(settings, isolated, call):
    (settings.workspace / 'a.py').write_text('value = 0\n')
    script = 'from a import value; assert value == 1; print("真实组合验收")'
    async def review(messages, tools, emit):
        assert '后端组合验收' in messages[-1]['content']
        assert '真实组合验收' in messages[-1]['content']
        assert script not in messages[-1]['content']
        assert (settings.workspace / 'a.py').read_text() == 'value = 0\n'
        return report()
    manager, _, _, _ = isolated([call('write_file', path='a.py', content='value = 1\n'), report(), review], script)
    try:
        await manager.dispatch('plan', tasks=[task('a', ['a.py']), task('review', ['.'], ['a'], 'reviewer')])
        assert manager.integration['status'] == 'integrated'
        assert (settings.workspace / 'a.py').read_text() == 'value = 1\n'
    finally:
        await manager.close()


def test_post_commit_archive_failure_does_not_hide_published_files(settings, monkeypatch):
    (settings.workspace / 'a.py').write_text('old\n')
    bundle = WorkspaceBundle(settings.workspace, settings.home, asyncio.Lock())
    candidate = bundle.fork('fix', set())
    (candidate / 'a.py').write_text('new\n')
    bundle.collect('fix', ['a.py'])
    _, tree = bundle.stage({'fix'})
    original = bundle.json
    def fail_archive(name, value):
        if name == 'baseline-1.json':
            raise OSError('模拟归档失败')
        return original(name, value)
    monkeypatch.setattr(bundle, 'json', fail_archive)
    result = bundle.publish(tree, {'fix'})
    assert result['status'] == 'integrated' and '归档失败' in result['reason']
    assert (settings.workspace / 'a.py').read_text() == 'new\n'
    assert json.loads(Path(result['journal']).read_text())['status'] == 'committed'
    bundle.cleanup()


async def test_cleanup_failure_does_not_leave_active_tasks(settings, isolated, call, monkeypatch):
    manager, bundle, events, _ = isolated([call('write_file', path='a.py', content='new\n'), report()])
    await manager.dispatch('plan', tasks=[task('a', ['a.py'])])
    original = bundle.cleanup
    def fail_cleanup():
        raise OSError('模拟清理失败')
    monkeypatch.setattr(bundle, 'cleanup', fail_cleanup)
    await manager.close()
    assert all(item.execution.done() for item in manager.tasks.values())
    assert any(event['type'] == 'error' and '清理失败' in event['message'] for event in events)
    original()


async def test_copy_shell_cannot_discover_parent_git_repository(settings, tmp_path):
    outer = tmp_path / 'outer'
    outer.mkdir()
    env = {**child_environment(), 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
    subprocess.run(['git', '-C', str(outer), 'init', '-q'], check=True, env=env)
    (outer / 'private-project-name.txt').write_text('user data')
    candidate = outer / 'results' / 'copy'
    candidate.mkdir(parents=True)
    result = await ExecuteCommand(candidate, settings).execute('git status --porcelain')
    assert not result.ok and result.evidence['exit_code'] != 0
    assert 'private-project-name' not in result.evidence['output']
    assert not (outer / '.git/index').exists()
    # 选择的工作区本身是仓库时，普通 Git 读取不受影响。
    own = await ExecuteCommand(outer, settings).execute('git status --porcelain')
    assert own.ok and 'private-project-name.txt' in own.evidence['output']
