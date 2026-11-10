"""显式工具注册：所有可用能力都能在这里看到，无插件扫描和隐式导入。"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mini_nanobot.config import Settings
from mini_nanobot.session import SessionStore
from mini_nanobot.task_graph import TASK_SCHEMA
from mini_nanobot.tools.base import Tool, ToolResult, parameters, text
from mini_nanobot.tools.filesystem import EditFile, ListDirectory, ReadFile, SearchFiles, WriteFile
from mini_nanobot.tools.shell import ExecuteCommand


class MemoryTool(Tool):
    def __init__(self, workspace: Path, store: SessionStore, write: bool = False):
        self.workspace, self.store, self.write = workspace, store, write
        self.name = "remember" if write else "read_memory"
        self.description = "仅在用户明确要求时覆盖项目长期记忆；保留已有有效内容，不保存密钥。" if write else "读取项目长期记忆。"
        self.read_only = not write
        self.parameters = parameters(["content"], content=text("完整的项目记忆")) if write else parameters()

    async def execute(self, content: str = "", **kwargs: Any) -> str:
        if self.write:
            self.store.write_memory(self.workspace, content)
            return "项目记忆已保存。"
        return self.store.read_memory(self.workspace) or "暂无项目记忆。"


class SubagentTool(Tool):
    name = "subagent"
    description = "管理子任务：plan 一次提交有依赖的任务计划，后端校验并按依赖调度；create 创建独立任务；check 查看；send 补充；cancel 取消。plan 中每项提供 id、role、task、depends_on、acceptance；下游自动收到前置任务交接。wait 默认 true。共享目录，请分配不同文件避免冲突。"
    parameters = parameters(
        ["action"], action={"type": "string", "enum": ["plan", "create", "check", "send", "cancel"]},
        tasks={"type": "array", "minItems": 1, "maxItems": 12, "items": TASK_SCHEMA},
        role={"type": "string", "enum": ["explorer", "coder", "reviewer"]},
        task=text("子任务目标、文件范围和验收要求"), task_id=text("已有子任务 ID"),
        message=text("补充说明"), wait={"type": "boolean"},
    )

    def __init__(self, handler: Callable[..., Awaitable[str]]):
        self.handler = handler

    async def execute(self, **arguments: Any) -> str:
        return await self.handler(**arguments)


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self.tools = {tool.name: tool for tool in tools}

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"工具错误：当前角色无权使用 {name}。", ok=False)
        return await tool.call(arguments)


def build_tools(
    workspace: Path, settings: Settings, store: SessionStore, write_lock: asyncio.Lock,
    role: str = "main", subagent: Callable[..., Awaitable[str]] | None = None,
) -> ToolRegistry:
    tools: list[Tool] = [ReadFile(workspace, write_lock), ListDirectory(workspace, write_lock), SearchFiles(workspace, write_lock), MemoryTool(workspace, store)]
    if role in {"main", "coder"}:
        tools.extend([WriteFile(workspace, write_lock), EditFile(workspace, write_lock), ExecuteCommand(workspace, settings)])
    if role == "main":
        tools.append(MemoryTool(workspace, store, write=True))
        if subagent:
            tools.append(SubagentTool(subagent))
    return ToolRegistry(tools)
