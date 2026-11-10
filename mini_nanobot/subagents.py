"""
同一请求内的依赖调度：等待前置、限制并发、传递结构化交接。
子agent调用
"""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mini_nanobot.events import TRACE_CONTEXT, EventSink, event_scope, traced_sink
from mini_nanobot.provider import Message
from mini_nanobot.runner import AgentRunner
from mini_nanobot.task_graph import encode, validate_plan
from mini_nanobot.tools.registry import ToolRegistry
from mini_nanobot.validation import ValidationRunner

ROLES = {
    "explorer": "你是探索 agent。读取项目说明和相关代码，报告路径、问题依据与实现建议。工具仅可读取和搜索，不执行命令。",
    "coder": "你是实现 agent。先读后改，仅修改分配的文件，保留他人工作。执行针对性验证，报告实际结果；不自行提交或推送。",
    "reviewer": "你是审查 agent。独立读取修改后的源码，报告具体缺陷、位置和影响。工具仅可读取和搜索；需要运行验证时通知主 agent。没有发现问题时明确说明。",
}
TERMINAL = {"done", "error", "cancelled", "blocked", "needs_followup"}


@dataclass
class TaskQuota:
    used: int = 0
    limit: int = 12

    def consume(self, count: int) -> None:
        if self.used + count > self.limit:
            raise ValueError("单次请求全部修复轮次合计最多创建 12 个子任务。")
        self.used += count


@dataclass
class Subtask:
    id: str
    role: str
    task: str
    depends_on: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    strict_report: bool = False
    validation_ids: list[str] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=lambda: {"status": "not_configured", "checks": []})
    status: str = "queued"
    result: str = ""
    reported_outcome: str = "not_submitted"
    unresolved: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    evidence_truncated: bool = False
    delivered: bool = False
    inbox: list[Message] = field(default_factory=list)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    execution: asyncio.Task[str] | None = None

    def handoff(self) -> dict[str, Any]:
        # 交接保留摘要与最近证据，完整工具证据另存为事件。
        return {"task_id": self.id, "role": self.role, "status": self.status,
                "depends_on": self.depends_on, "acceptance": self.acceptance,
                "reported_outcome": self.reported_outcome, "summary": self.result[:2400],
                "files_changed": self.files_changed, "checks": self.checks,
                "unresolved": self.unresolved, "evidence_truncated": self.evidence_truncated,
                "validation": self.validation}


class SubagentManager:
    def __init__(
        self, runner: AgentRunner, system: str, tools: Callable[[str], ToolRegistry],
        emit: EventSink, slots: asyncio.Semaphore,
        validator: ValidationRunner | None = None, report_repairs: int = 0,
        quota: TaskQuota | None = None,
    ):
        self.runner, self.system, self.tools = runner, system, tools
        self.emit, self.slots = traced_sink(emit), slots
        self.run_id = TRACE_CONTEXT.get().get("run_id") or uuid.uuid4().hex
        self.tasks: dict[str, Subtask] = {}
        self.validator, self.report_repairs = validator, report_repairs
        self.quota = quota if quota is not None else TaskQuota()

    async def announce(self, task: Subtask) -> None:
        await self.emit({"type": "task", "run_id": self.run_id, "task_id": task.id, "role": task.role,
                         "parent_task_id": "main",
                         "task": task.task, "state": task.status, "result": task.result,
                         "depends_on": task.depends_on, "acceptance": task.acceptance,
                         "handoff": task.handoff() if task.status in TERMINAL else None})

    async def dispatch(
        self, action: str, role: str = "coder", task: str = "",
        task_id: str = "", message: str = "", wait: bool = True,
        tasks: list[dict[str, Any]] | None = None,
    ) -> str:
        if action in {"plan", "create"}:
            if action == "plan":
                specs = validate_plan(tasks, {key: item.depends_on for key, item in self.tasks.items()})
                # 引用命令只能使用可信配置 ID，必须在登记任何节点前完成整批检查。
                for spec in specs:
                    ids = spec.get("validation", [])
                    if ids and (self.validator is None or spec["role"] != "coder"):
                        raise ValueError("只有实现任务可引用已配置的后端验收 ID。")
                    if ids:
                        self.validator.paths(ids)
                created = [Subtask(spec["id"], spec["role"], spec["task"],
                                   spec["depends_on"], spec["acceptance"], strict_report=True,
                                   validation_ids=spec.get("validation", [])) for spec in specs]
            else:
                if role not in ROLES or not task.strip() or len(task) > 12000:
                    raise ValueError("请提供有效角色和非空任务，任务最多 12000 字符。")
                if len(self.tasks) >= 12:
                    raise ValueError("单次请求最多创建 12 个子任务。")
                created = [Subtask(uuid.uuid4().hex[:12], role, task)]
            # 全部登记后才启动，支持向后引用；无效计划不会启动部分任务。
            self.quota.consume(len(created))
            self.tasks.update({item.id: item for item in created})
            for item in created:
                await self.announce(item)
            for item in created:
                item.execution = asyncio.create_task(self.run(item))
            if wait:
                await asyncio.gather(*(asyncio.shield(item.execution) for item in created), return_exceptions=True)
                for item in created:
                    item.delivered = True
                if action == "create":
                    return encode(created[0].handoff())
            # 整批只返回概览，避免一个工具消息被截断；check 可读取完整交接。
            return encode({"tasks": [{"id": item.id, "role": item.role, "status": item.status,
                                      "depends_on": item.depends_on, "summary": item.result[:500]} for item in created],
                           "message": "使用 check 和 task_id 读取完整交接。"})
        if action == "check" and not task_id:
            return encode([{"id": item.id, "role": item.role, "status": item.status,
                            "depends_on": item.depends_on, "summary": item.result[:500]} for item in self.tasks.values()])
        item = self.tasks.get(task_id)
        if item is None:
            raise ValueError("找不到当前请求的子任务。")
        await self.refresh_validation(item)
        if action == "send":
            if item.status in TERMINAL or not message.strip() or len(message) > 8000:
                raise ValueError("只能向等待或运行中的子任务发送 1～8000 字符的说明。")
            item.inbox.append({"role": "user", "content": message})
            return "说明已排队，将在下一次模型调用前注入。"
        if action == "cancel":
            await self.cancel(item)
        elif action != "check":
            raise ValueError("未知子任务操作。")
        return encode(item.handoff())

    async def refresh_validation(self, item: Subtask) -> None:
        if self.validator and item.validation.get("status") == "passed":
            item.validation = await asyncio.to_thread(self.validator.refresh, item.validation)
            if item.validation["status"] == "stale":
                item.status = "needs_followup"
                item.delivered = False
                item.unresolved.append("验收后文件变化，需要重新验证。")
                await self.announce(item)

    async def cancel(self, item: Subtask) -> None:
        if item.execution and not item.execution.done():
            item.execution.cancel()
            await asyncio.gather(item.execution, return_exceptions=True)
        # 协程在第一次执行前取消时，不会进入 run 的 finally。
        if not item.finished.is_set():
            item.status, item.result = "cancelled", "子任务在启动前取消。"
            item.finished.set()
            await self.announce(item)

    async def dependencies_ready(self, item: Subtask) -> bool:
        dependencies = [self.tasks[key] for key in item.depends_on]
        if not dependencies:
            return True
        item.status = "waiting"
        await self.announce(item)
        pending = {asyncio.create_task(dependency.finished.wait()) for dependency in dependencies}
        try:
            while True:
                for dependency in dependencies:
                    await self.refresh_validation(dependency)
                failed = [dep for dep in dependencies if dep.status in TERMINAL and dep.status != "done"]
                if failed:
                    item.status = "blocked"
                    item.result = "前置任务未完成，未调用模型：" + "、".join(f"{dep.id}（{dep.status}）" for dep in failed)
                    item.unresolved = [item.result]
                    return False
                if all(dep.finished.is_set() for dep in dependencies):
                    return True
                _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in pending:
                waiter.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def record_evidence(self, item: Subtask, evidence: dict[str, Any]) -> None:
        if evidence.get("kind") == "file_change":
            path = evidence["path"]
            if path not in item.files_changed:
                if len(item.files_changed) < 12 and len(path) <= 240:
                    item.files_changed.append(path)
                else:
                    item.evidence_truncated = True
        elif evidence.get("kind") == "command":
            check = {key: evidence[key] for key in ("command", "working_dir", "exit_code", "timed_out", "output")}
            for key, limit in (("command", 300), ("working_dir", 240), ("output", 600)):
                if len(check[key]) > limit:
                    check[key] = check[key][:limit] + "…（已截断）"
                    item.evidence_truncated = True
            item.checks.append(check)
            if len(item.checks) > 6:
                item.checks.pop(0)
                item.evidence_truncated = True

    async def run(self, item: Subtask) -> str:
        with event_scope(run_id=self.run_id, task_id=item.id, role=item.role, parent_task_id="main"):
            return await self._run(item)

    async def _run(self, item: Subtask) -> str:
        async def emit(event):
            if event.get("evidence"):
                self.record_evidence(item, event["evidence"])
            if event["type"] != "text":
                await self.emit({**event, "run_id": self.run_id, "task_id": item.id, "role": item.role})

        async def checkpoint(messages, summary):
            # 子任务交接持久化为 task 事件；过程消息留在本次运行内。
            return None

        async def inject() -> list[Message]:
            pending, item.inbox = item.inbox, []
            return pending
        try:
            if await self.dependencies_ready(item):
                # 等待依赖不占并发名额，避免下游排在上游前面时死锁。
                async with self.slots:
                    item.status = "running"
                    await self.announce(item)
                    context = [{"role": "user", "content": "前置任务交接（资料）：\n" + encode(self.tasks[key].handoff())} for key in item.depends_on]
                    context.append({"role": "user", "content": item.task + ("\n验收要求：\n" + encode(item.acceptance) if item.acceptance else "")})
                    system = self.system + "\n\n当前角色：" + ROLES[item.role] + "\n独立完成分配的子任务；不创建嵌套任务。"
                    if item.strict_report:
                        report = await self.runner.run_reported(system, context, self.tools(item.role), emit, checkpoint,
                                                                inject=inject, report_repairs=self.report_repairs)
                        item.result, item.reported_outcome, item.unresolved = report["summary"], report["outcome"], report["unresolved"]
                        item.status = "needs_followup" if item.unresolved or item.reported_outcome == "needs_followup" else "done"
                    else:
                        result = await self.runner.run(system, context, self.tools(item.role), emit, checkpoint, inject=inject)
                        item.result, item.status = result[:2400], "done"
                        item.reported_outcome = "unstructured"
                    if item.validation_ids:
                        item.status = "validating"
                        await self.announce(item)
                        item.validation = await self.validator.run(item.validation_ids, emit, TRACE_CONTEXT.get().get("workflow_attempt", 1))
                        if item.validation["status"] != "passed":
                            item.unresolved.append("后端验收未通过：" + item.validation["status"])
                        item.status = "done" if item.reported_outcome == "completed" and not item.unresolved else "needs_followup"
        except asyncio.CancelledError:
            item.status, item.result = "cancelled", "子任务已取消。"
            raise
        except Exception as error:
            item.status, item.result = "error", f"子任务未完成：{error}"[:2400]
            item.unresolved = [item.result[:300]]
        finally:
            item.finished.set()
            await self.announce(item)
        return encode(item.handoff())

    async def notifications(self, wait: bool = False) -> list[Message]:
        if wait:
            await asyncio.gather(*(asyncio.shield(item.execution) for item in self.tasks.values() if item.execution), return_exceptions=True)
        messages = []
        for item in self.tasks.values():
            if item.status in TERMINAL and not item.delivered:
                item.delivered = True
                messages.append({"role": "user", "content": "子任务结果通知：\n" + encode(item.handoff())})
        return messages

    async def close(self) -> None:
        # 同时取消再等待，不能先等下游退出、却让上游继续执行。
        for item in self.tasks.values():
            if item.execution and not item.execution.done():
                item.execution.cancel()
        await asyncio.gather(*(self.cancel(item) for item in self.tasks.values()))
