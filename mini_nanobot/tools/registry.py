"""
负责组装当前角色可用的工具，把工具说明提供给模型，再根据模型返回的名称找到工具执行。
显式工具注册：所有可用能力都能在这里看到，无插件扫描和隐式导入。

本质上就是选择、组织和查找工具
"""

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


#创建读记忆和写记忆两种工具
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


#子agent工具，调用时会把参数传给外部的handler函数，返回值是字符串
class SubagentTool(Tool):
    name = "subagent"
    description = "管理子任务。先探索，默认直接完成小型或紧密耦合任务；明确独立工作、文件归属和验收条件且协作收益值得额外开销时再委派，用户指定方式时遵循要求。plan 提交依赖计划；create 创建独立任务；check 查看；send 补充；cancel 取消。计划项含 id、role、task、depends_on、acceptance，实现任务请明确 write_paths 文件或目录范围，可用 validation 引用可信验收 ID。独立副本模式由后端采集真实补丁、检查冲突、组合验收后发布；完成声明不等于已发布。wait 默认 true。"
    parameters = parameters(
        ["action"], action={"type": "string", "enum": ["plan", "create", "check", "send", "cancel"]},
        tasks={"type": "array", "minItems": 1, "maxItems": 12, "items": TASK_SCHEMA},
        role={"type": "string", "enum": ["explorer", "coder", "reviewer"]},
        task=text("子任务目标、文件范围和验收要求"), task_id=text("已有子任务 ID"),
        write_paths={"type": "array", "minItems": 1, "maxItems": 30, "items": {"type": "string", "minLength": 1}},
        message=text("补充说明"), wait={"type": "boolean"},
    )

    def __init__(self, handler: Callable[..., Awaitable[str]]):
        self.handler = handler

    async def execute(self, **arguments: Any) -> str:
        return await self.handler(**arguments)


class ToolRegistry:
    def __init__(self, tools: list[Tool]):
        self.tools = {tool.name: tool for tool in tools}

    #这个方法是给模型用的，返回所有工具的说明，我觉得本质上依旧是继承了tool的schema方法
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"工具错误：当前角色无权使用 {name}。", ok=False)
        #这里面的call可是执行啊
        return await tool.call(arguments)

#为当前角色组装工具。
def build_tools(
    workspace: Path, settings: Settings, store: SessionStore, write_lock: asyncio.Lock,
    role: str = "main", subagent: Callable[..., Awaitable[str]] | None = None,
    write_paths: list[str] | None = None, memory_workspace: Path | None = None,
) -> ToolRegistry:
    tools: list[Tool] = [ReadFile(workspace, write_lock), ListDirectory(workspace, write_lock), SearchFiles(workspace, write_lock), MemoryTool(memory_workspace or workspace, store)]
    if role in {"main", "coder"}:
        tools.extend([WriteFile(workspace, write_lock, write_paths), EditFile(workspace, write_lock, write_paths), ExecuteCommand(workspace, settings)])
    if role == "main":
        tools.append(MemoryTool(workspace, store, write=True))
        if subagent:
            tools.append(SubagentTool(subagent))
    return ToolRegistry(tools)
