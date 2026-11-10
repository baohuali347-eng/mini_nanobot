"""离线模型替身：测试真实执行链路，不消耗外部模型额度。"""
import copy
import json

import pytest

from mini_nanobot.config import Settings
from mini_nanobot.events import discard_event


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def complete(self, messages, tools=None, emit=discard_event):
        self.requests.append(copy.deepcopy(messages))
        response = self.responses.pop(0)
        if callable(response):
            response = await response(messages, tools, emit)
        if response.get('content'):
            await emit({'type': 'text', 'text': response['content']})
        return copy.deepcopy(response)

    async def close(self):
        pass


@pytest.fixture
def settings(tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    return Settings(home=tmp_path / 'profile', workspace=workspace, api_key='test-only-key')


@pytest.fixture
def model_factory():
    return ScriptedModel


@pytest.fixture
def call():
    def make(name, **arguments):
        return {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'call-' + name, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments),
            },
        }]}
    return make
