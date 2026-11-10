"""验证旧配置读取、密钥引用、项目记忆与中断检查点。"""
import json

import pytest
from typer.testing import CliRunner

from mini_nanobot.agent import AgentService
from mini_nanobot.cli import app
from mini_nanobot.config import load_settings
from mini_nanobot.session import SessionStore


def test_legacy_config_reads_only_needed_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('MINI_NANOBOT_API_KEY', 'test-key')
    home = tmp_path / 'profile'
    home.mkdir()
    (home / 'config.json').write_text(json.dumps({
        'agents': {'defaults': {'model': 'deepseek-flash', 'provider': 'custom', 'maxConcurrentSubagents': 3}},
        'providers': {'custom': {'apiBase': 'https://api.deepseek.com', 'apiKey': '${MINI_NANOBOT_API_KEY}'}},
        'channels': {'telegram': {'enabled': True}},
    }))
    settings = load_settings(home)
    assert settings.api_key == 'test-key'
    assert settings.max_subagents == 3
    assert 'test-key' not in repr(settings)
    monkeypatch.delenv('MINI_NANOBOT_API_KEY')
    with pytest.raises(ValueError, match='缺少环境变量'):
        load_settings(home)


def test_session_and_memory_survive_restart_without_modifying_legacy_database(settings):
    settings.home.mkdir()
    old = settings.home / 'sessions.sqlite3'
    old.write_bytes(b'legacy-database-untouched')
    store = SessionStore(settings.home)
    item = store.create(settings.workspace)
    store.save(item['id'], [{'role': 'user', 'content': '测试任务'}], '已完成部分调研', 'running')
    store.write_memory(settings.workspace, '使用中文解释。')
    store.close()
    reopened = SessionStore(settings.home)
    try:
        assert reopened.get(item['id'])['status'] == 'interrupted'
        assert reopened.get(item['id'])['summary'] == '已完成部分调研'
        assert reopened.read_memory(settings.workspace) == '使用中文解释。'
        assert old.read_bytes() == b'legacy-database-untouched'
    finally:
        reopened.close()


async def test_one_process_per_profile_prevents_false_crash_recovery(settings, model_factory):
    first = AgentService(settings, model_factory([]))
    try:
        with pytest.raises(ValueError, match='已有助手运行'):
            AgentService(settings, model_factory([]))
    finally:
        await first.close()


def test_init_is_idempotent_and_contains_no_actual_key(tmp_path):
    runner = CliRunner()
    args = ['--home', str(tmp_path / 'profile'), 'init', '--if-missing']
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    before = (tmp_path / 'profile/config.json').read_text()
    assert '${MINI_NANOBOT_API_KEY}' in before
    assert runner.invoke(app, args).exit_code == 0
    assert (tmp_path / 'profile/config.json').read_text() == before
