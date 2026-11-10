"""验证文件边界、角色权限，以及命令取消后的实际进程行为。"""
import asyncio
import shlex
import sys

import pytest

from mini_nanobot.session import SessionStore
from mini_nanobot.tools.registry import build_tools
from mini_nanobot.tools.shell import ExecuteCommand


@pytest.mark.parametrize('role', ['explorer', 'reviewer'])
async def test_read_only_roles_can_search_but_cannot_write_or_execute(settings, role):
    store = SessionStore(settings.home)
    source = settings.workspace / 'app.py'
    source.write_text('VALUE = 1\n')
    tools = build_tools(settings.workspace, settings, store, asyncio.Lock(), role)
    try:
        assert 'VALUE' in await tools.execute('read_file', {'path': 'app.py'})
        assert 'app.py:1' in await tools.execute('search', {'query': 'VALUE'})
        assert '无权' in await tools.execute('write_file', {'path': 'app.py', 'content': 'bad'})
        assert '无权' in await tools.execute('exec', {'command': 'echo bad'})
        assert source.read_text() == 'VALUE = 1\n'
    finally:
        store.close()


async def test_coder_edits_exact_text_and_preserves_file_mode(settings):
    store = SessionStore(settings.home)
    path = settings.workspace / 'run.py'
    path.write_text('VALUE = 1\n')
    path.chmod(0o755)
    tools = build_tools(settings.workspace, settings, store, asyncio.Lock(), 'coder')
    try:
        assert '已修改' in await tools.execute('edit_file', {'path': 'run.py', 'old': 'VALUE = 1', 'new': 'VALUE = 2'})
        assert path.read_text() == 'VALUE = 2\n'
        assert path.stat().st_mode & 0o777 == 0o755
        assert '工具错误' in await tools.execute('edit_file', {'path': 'run.py', 'old': 'VALUE = 1', 'new': 'oops'})
        assert '无权' in await tools.execute('subagent', {'action': 'create', 'task': 'nested'})
    finally:
        store.close()


async def test_filesystem_blocks_traversal_symlinks_and_credentials(settings, tmp_path):
    outside = tmp_path / 'secret.txt'
    outside.write_text('private')
    (settings.workspace / 'link').symlink_to(outside)
    (settings.workspace / '.env').write_text('KEY=test-only')
    store = SessionStore(settings.home)
    tools = build_tools(settings.workspace, settings, store, asyncio.Lock())
    try:
        for path in ['../secret.txt', str(outside), 'link', '.env']:
            assert '工具错误' in await tools.execute('read_file', {'path': path})
        assert '工具错误' in await tools.execute('write_file', {'path': 'link', 'content': 'changed'})
        assert outside.read_text() == 'private'
        assert 'secret' not in await tools.execute('search', {'query': 'private'})
        assert '工具错误' in await tools.execute('read_file', {'path': 123})
    finally:
        store.close()


async def test_shell_does_not_inherit_model_secrets(settings, monkeypatch):
    monkeypatch.setenv('MINI_NANOBOT_API_KEY', 'test-only-secret')
    command = shlex.join([sys.executable, '-c', "import os; print('MINI_NANOBOT_API_KEY' in os.environ)"])
    output = await ExecuteCommand(settings.workspace, settings).execute(command)
    assert 'False' in output


async def test_cancelling_command_kills_children(settings):
    command = shlex.join([sys.executable, '-c', "import pathlib,time; pathlib.Path('started').touch(); time.sleep(1); pathlib.Path('finished').touch()"])
    task = asyncio.create_task(ExecuteCommand(settings.workspace, settings).execute(command))
    for _ in range(100):
        if (settings.workspace / 'started').exists():
            break
        await asyncio.sleep(.01)
    assert (settings.workspace / 'started').exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.1)
    assert not (settings.workspace / 'finished').exists()


async def test_sandbox_failure_does_not_run_unprotected(settings, monkeypatch):
    settings.sandbox = 'bwrap'
    monkeypatch.setattr('mini_nanobot.sandbox.wrap_command', lambda *args, **kwargs: 'missing-mini-sandbox-backend-12345')
    output = await ExecuteCommand(settings.workspace, settings).execute('echo unsafe > marker')
    assert '127' in output
    assert not (settings.workspace / 'marker').exists()
