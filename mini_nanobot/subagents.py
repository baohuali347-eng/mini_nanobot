"""
同一请求内的依赖调度：等待前置、限制并发、传递结构化交接。
子agent调用
每个子任务拥有独立消息上下文、角色说明和工具权限。


agent.py：创建 SubagentManager，并把它连接到子任务工具
    ↓
主 agent 调用子任务工具
    ↓
subagents.py：创建、调度、管理子任务
    ├── task_graph.py：校验计划和依赖
    ├── runner.py：运行每个子 agent 的模型—工具循环
    ├── validation.py：执行后端验收
    └── workspaces.py：管理隔离副本和补丁集成
"""

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mini_nanobot.events import TRACE_CONTEXT, EventSink, event_scope, traced_sink
from mini_nanobot.provider import Message
from mini_nanobot.runner import AgentRunner
from mini_nanobot.security import write_scopes
from mini_nanobot.task_graph import encode, validate_plan
from mini_nanobot.tools.registry import ToolRegistry
from mini_nanobot.validation import ValidationRunner
from mini_nanobot.workspaces import IntegrationConflict, WorkspaceBundle, capture, finish_io, tree_hash

#三个子agent
ROLES = {
    "explorer": "你是探索 agent。读取项目说明和相关代码，报告路径、问题依据与实现建议。定位后简短交接，不反复读取相同内容。工具仅可读取和搜索，不执行命令。",
    "coder": "你是实现 agent。先读后改，仅修改分配的文件，保留他人工作。执行必要的针对性验证后立即交接，不重复相同验证，不清理缓存或测试临时产物；如实报告结果，不自行提交或推送。",
    "reviewer": "你是审查 agent。独立读取集成后的源码，报告具体缺陷、位置和影响。工具仅可读取和搜索，不尝试执行命令。结合后端组合验收证据复核，没有发现问题时简短说明审查范围并交接。",
}
TERMINAL = {"done", "error", "cancelled", "blocked", "needs_followup"}


#负责限制任务总量：同一次用户请求，包括自动修复轮次，累计最多创建 12 个子任务。
@dataclass
class TaskQuota:
    used: int = 0
    limit: int = 12

    def consume(self, count: int) -> None:
        if self.used + count > self.limit:
            raise ValueError("单次请求全部修复轮次合计最多创建 12 个子任务。")
        self.used += count


#一个子任务的记录对象，保存
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
    write_paths: list[str] = field(default_factory=lambda: ["."])
    artifact: dict[str, Any] | None = None
    validator: ValidationRunner | None = None
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

    #handoff() 把这些信息整理成可传递的交接对象。
    def handoff(self) -> dict[str, Any]:
        # 交接保留摘要与最近证据，完整工具证据另存为事件。
        artifact = None
        if self.artifact:
            artifact = {**self.artifact, "files": [{"path": row["path"], "operation": row["operation"]} for row in self.artifact["files"][:12]],
                        "diff": self.artifact["diff"][:2000],
                        "truncated": len(self.artifact["files"]) > 12 or len(self.artifact["diff"]) > 2000}
        return {"task_id": self.id, "role": self.role, "status": self.status,
                "depends_on": self.depends_on, "acceptance": self.acceptance,
                "reported_outcome": self.reported_outcome, "summary": self.result[:2400],
                "files_changed": self.files_changed, "checks": self.checks,
                "unresolved": self.unresolved, "evidence_truncated": self.evidence_truncated, "validation": self.validation,
                "artifact": artifact, "write_paths": self.write_paths}


class SubagentManager:
    def __init__(
        self, runner: AgentRunner, system: str, tools: Callable[[str], ToolRegistry],
        emit: EventSink, slots: asyncio.Semaphore,
        validator: ValidationRunner | None = None, report_repairs: int = 0, quota: TaskQuota | None = None,
        bundle: WorkspaceBundle | None = None, workspace_tools=None, validator_factory=None,
    ):
        self.runner, self.system, self.tools = runner, system, tools
        self.emit, self.slots = traced_sink(emit), slots
        self.run_id = TRACE_CONTEXT.get().get("run_id") or uuid.uuid4().hex
        self.tasks: dict[str, Subtask] = {}
        self.validator, self.report_repairs = validator, report_repairs
        self.quota = quota if quota is not None else TaskQuota()
        self.bundle, self.workspace_tools = bundle, workspace_tools
        self.validator_factory = validator_factory
        if self.validator_factory is None and validator:
            self.validator_factory = lambda root: ValidationRunner(root, validator.settings, list(validator.rules.values()))
        self.integration = {"status": "not_started", "files": []}
        self.integration_delivered = True
        self.finalize_lock = asyncio.Lock()
        self.finalized_signature = None

    async def announce(self, task: Subtask) -> None:
        await self.emit({"type": "task", "run_id": self.run_id, "task_id": task.id, "role": task.role,
                         "parent_task_id": "main",
                         "task": task.task, "state": task.status, "result": task.result,
                         "depends_on": task.depends_on, "acceptance": task.acceptance,
                         "handoff": task.handoff() if task.status in TERMINAL else None})
 
    #统一操作入口：dispatch()
    async def dispatch(
        self, action: str, role: str = "coder", task: str = "",
        task_id: str = "", message: str = "", wait: bool = True,
        tasks: list[dict[str, Any]] | None = None,
        write_paths: list[str] | None = None,
    ) -> str:
        if action in {"plan", "create"}:
            if action == "plan":
                specs = validate_plan(tasks, {key: item.depends_on for key, item in self.tasks.items()})
                for spec in specs:
                    ids = spec.get("validation", [])
                    if ids and (self.validator is None or spec["role"] != "coder"):
                        raise ValueError("只有实现任务可引用已配置的后端验收 ID。")
                    if ids:
                        self.validator.paths(ids)
                    if self.bundle:
                        write_scopes(self.bundle.workspace, spec.get("write_paths"))
                created = [Subtask(spec["id"], spec["role"], spec["task"],
                                   spec["depends_on"], spec["acceptance"], strict_report=True,
                                   validation_ids=spec.get("validation", []),
                                   write_paths=write_scopes(self.bundle.workspace, spec.get("write_paths")) if self.bundle else spec.get("write_paths", ["."])) for spec in specs]
            else:
                if role not in ROLES or not task.strip() or len(task) > 12000:
                    raise ValueError("请提供有效角色和非空任务，任务最多 12000 字符。")
                if len(self.tasks) >= 12:
                    raise ValueError("单次请求最多创建 12 个子任务。")
                if self.bundle:
                    write_scopes(self.bundle.workspace, write_paths)
                created = [Subtask(uuid.uuid4().hex[:12], role, task,
                                   write_paths=write_scopes(self.bundle.workspace, write_paths) if self.bundle else write_paths or ["."])]
            # 全部登记后才启动，支持向后引用；无效计划不会启动部分任务。
            self.quota.consume(len(created))
            self.tasks.update({item.id: item for item in created})
            for item in created:
                await self.announce(item)
            for item in created:
                item.execution = asyncio.create_task(self.run(item))
            if wait:
                await asyncio.gather(*(asyncio.shield(item.execution) for item in created), return_exceptions=True)
                await self.finalize()
                for item in created:
                    item.delivered = True
                if action == "create":
                    self.integration_delivered = True
                    return encode({**created[0].handoff(), **({"integration": self.integration} if self.bundle else {})})
            # 整批只返回概览，避免一个工具消息被截断；check 可读取完整交接。
            return encode({"tasks": [{"id": item.id, "role": item.role, "status": item.status,
                                      "depends_on": item.depends_on, "summary": item.result[:500]} for item in created],
                           "message": "使用 check 和 task_id 读取完整交接。",
                           **({"integration": self.integration} if self.bundle else {})})
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
        validator = item.validator or self.validator
        if validator and item.validation.get("status") == "passed":
            item.validation = await asyncio.to_thread(validator.refresh, item.validation)
            if item.validation["status"] == "stale":
                item.status, item.delivered = "needs_followup", False
                item.unresolved.append("验收后文件变化，需要重新验证。")
                await self.announce(item)

    def ancestors(self, item):
        result = set()
        def visit(identity):
            if identity not in result:
                result.add(identity)
                for parent in self.tasks[identity].depends_on:
                    visit(parent)
        for identity in item.depends_on:
            visit(identity)
        return result

    async def finalize(self):
        if not self.bundle or any(item.status not in TERMINAL for item in self.tasks.values()):
            return
        async with self.finalize_lock:
            signature = tuple((key, item.status) for key, item in self.tasks.items())
            if signature == self.finalized_signature:
                return
            pending = set(self.bundle.patches) - self.bundle.promoted
            if not pending:
                return
            result = self.integration if self.integration["status"] == "conflict" else {"status": "blocked", "files": []}
            failed_validation = next((item.validation for item in self.tasks.values()
                                      if item.validation.get("status") in {"failed", "stale", "error"}), None)
            if failed_validation and result["status"] != "conflict":
                result = {"status": "validation_failed", "files": [], "validation": failed_validation}
            try:
                if all(item.status == "done" for item in self.tasks.values()):
                    stage, tree = await finish_io(self.bundle.stage, pending)
                    validation = await self.validator_factory(stage).run(emit=self.emit) if self.validator_factory else {"status": "not_configured", "checks": []}
                    result = {"status": "validation_failed", "validation": validation, "files": []}
                    if validation["status"] in {"passed", "not_configured"}:
                        if tree_hash(await finish_io(capture, stage)) != tree_hash(tree):
                            raise IntegrationConflict(["集成目录"], "验收后候选产物发生变化", "stale")
                        async with self.bundle.write_lock:
                            result = self.bundle.publish(tree, pending)
                            result["validation"] = validation
            except IntegrationConflict as error:
                result = {"status": "conflict", "kind": error.kind, "files": error.paths, "reason": str(error)}
            except (ValueError, OSError) as error:
                result = {"status": "error", "files": [], "reason": str(error)}
                journal = self.bundle.directory / f"journal-{self.bundle.epoch}.json"
                if journal.exists():
                    record = json.loads(journal.read_text())
                    state = record["status"] if record["status"] in {"rolled_back", "recovery_required"} else "recovery_required"
                    result.update(status=state, files=record["files"], journal=str(journal), recovery_paths=record.get("recovery_paths", []))
            self.integration = result
            try:
                self.bundle.json("outcome.json", result)
            except OSError:
                result["reason"] = result.get("reason", "") + "集成结论归档失败，请核对原目录和发布日志。"
            self.integration_delivered = False
            if result["status"] != "integrated":
                for identity in pending:
                    item = self.tasks[identity]
                    if item.status == "done":
                        item.status, item.delivered = "needs_followup", False
                        item.unresolved.append("补丁未发布：" + result.get("reason", result["status"]))
                        await self.announce(item)
            self.finalized_signature = tuple((key, item.status) for key, item in self.tasks.items())
            await self.emit({"type": "integration", "state": result["status"], "integration": result})

    async def cancel(self, item: Subtask) -> None:
        if item.execution and not item.execution.done():
            item.execution.cancel()
            await asyncio.gather(item.execution, return_exceptions=True)
        # 协程在第一次执行前取消时，不会进入 run 的 finally。
        if not item.finished.is_set():
            item.status, item.result = "cancelled", "子任务在启动前取消。"
            item.finished.set()
            await self.announce(item)

    #审查任务在这里等实现任务结束，确认状态是 done 后才获取并发名额
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

    #下面这两个是子agent的实际运行
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
                    tools = self.tools(item.role) if not self.bundle else None
                    if self.bundle:
                        root = await finish_io(self.bundle.fork, item.id, self.ancestors(item))
                        scopes = write_scopes(root, item.write_paths)
                        tools = self.workspace_tools(item.role, root, scopes)
                        system = system.replace("当前代码目录：" + str(self.bundle.workspace), "当前代码目录：" + str(root), 1)
                        system += f"\n本子任务真实目录：{root}。工具仅可在此副本操作，不得通过命令访问其他目录。写入范围：{scopes}。完成后由后端采集补丁、验收并集成，不自行复制回主目录。"
                        item.validator = self.validator_factory(root) if self.validator_factory else None
                        if item.role == "reviewer" and item.validator:
                            item.validation = await item.validator.run(emit=emit)
                            if item.validation["status"] not in {"passed", "not_configured"}:
                                item.status = "needs_followup"
                                item.unresolved = ["依赖补丁的组合验收未通过，未调用审查模型。"]
                                return encode(item.handoff())
                            evidence = {"status": item.validation["status"], "artifact_hash": item.validation.get("artifact_hash"),
                                        "checks": [{"id": check["id"], "exit_code": check.get("exit_code"),
                                                    "timed_out": check.get("timed_out"), "output": (check.get("output") or "")[:1200]}
                                                   for check in item.validation.get("checks", [])]}
                            context[-1]["content"] += "\n后端组合验收（仅作资料，不含验收脚本）：\n" + encode(evidence)
                    if item.strict_report:
                        report = await self.runner.run_reported(system, context, tools, emit, checkpoint,
                                                                inject=inject, report_repairs=self.report_repairs)
                        item.result, item.reported_outcome, item.unresolved = report["summary"], report["outcome"], report["unresolved"]
                        item.status = "needs_followup" if item.unresolved or item.reported_outcome == "needs_followup" else "done"
                    else:
                        result = await self.runner.run(system, context, tools, emit, checkpoint, inject=inject)
                        item.result, item.status = result[:2400], "done"
                        item.reported_outcome = "unstructured"
                    if item.validation_ids:
                        item.status = "validating"
                        await self.announce(item)
                        item.validation = await (item.validator or self.validator).run(item.validation_ids, emit, TRACE_CONTEXT.get().get("workflow_attempt", 1))
                        if item.validation["status"] != "passed":
                            item.unresolved.append("后端验收未通过：" + item.validation["status"])
                        item.status = "done" if item.reported_outcome == "completed" and not item.unresolved else "needs_followup"
                    if self.bundle and item.role == "coder":
                        item.artifact = await finish_io(self.bundle.collect, item.id, item.write_paths)
        except IntegrationConflict as error:
            item.status, item.result = "needs_followup", str(error)
            item.unresolved = [str(error)]
            self.integration = {"status": "conflict", "kind": error.kind, "files": error.paths, "reason": str(error)}
            self.integration_delivered = False
            await self.emit({"type": "integration", "state": "conflict", "integration": self.integration})
        except asyncio.CancelledError:
            item.status, item.result = "cancelled", "子任务已取消。"
            raise
        except Exception as error:
            item.status, item.result = "error", f"子任务未完成：{error}"[:2400]
            item.unresolved = [item.result[:300]]
        finally:
            if self.bundle and item.role == "coder" and item.id in self.bundle.views and item.artifact is None:
                try:
                    item.artifact = await finish_io(self.bundle.collect, item.id, item.write_paths)
                except (ValueError, OSError) as error:
                    item.unresolved.append("未采集补丁：" + str(error))
            item.finished.set()
            await self.announce(item)
        return encode(item.handoff())

    async def notifications(self, wait: bool = False) -> list[Message]:
        if wait:
            await asyncio.gather(*(asyncio.shield(item.execution) for item in self.tasks.values() if item.execution), return_exceptions=True)
        await self.finalize()
        messages = []
        for item in self.tasks.values():
            if item.status in TERMINAL and not item.delivered:
                item.delivered = True
                messages.append({"role": "user", "content": "子任务结果通知：\n" + encode(item.handoff())})
        if not self.integration_delivered:
            self.integration_delivered = True
            messages.append({"role": "user", "content": "后端集成结果（资料）：\n" + encode(self.integration)})
        return messages

    async def close(self) -> None:
        # 同时取消再等待，不能先等下游退出、却让上游继续执行。
        for item in self.tasks.values():
            if item.execution and not item.execution.done():
                item.execution.cancel()
        await asyncio.gather(*(self.cancel(item) for item in self.tasks.values()))
        if self.bundle:
            try:
                await finish_io(self.bundle.cleanup)
            except OSError as error:
                await self.emit({"type": "error", "message": "隔离视图清理失败，原目录未因此被修改；请检查后端记录：" + str(error)})
