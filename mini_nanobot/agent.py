"""
请求协调层：会话归属、运行/停止、检查点，以及主 agent 与子任务的连接。
本质上属于建立进行agent运行的上下文
"""

import asyncio
import uuid
from pathlib import Path

from filelock import FileLock, Timeout

from mini_nanobot.config import Settings
from mini_nanobot.context import build_system, repair_history
from mini_nanobot.events import EventSink, discard_event, event_scope, redact, traced_sink
from mini_nanobot.provider import BudgetedProvider, ModelProvider
from mini_nanobot.runner import AgentRunner
from mini_nanobot.security import project_path
from mini_nanobot.session import SessionStore
from mini_nanobot.subagents import SubagentManager, TaskQuota
from mini_nanobot.tools.registry import build_tools
from mini_nanobot.validation import ValidationRunner, validation_loop


def visible_messages(messages):
    return [{"role": row["role"], "content": row["content"]} for row in messages if row["role"] in {"user", "assistant"} and row.get("content")]


#agent调用入口
class AgentService:
    def __init__(self, settings: Settings, provider: ModelProvider | None = None):
        self.settings = settings
        settings.home.mkdir(parents=True, exist_ok=True)
        self.lease = FileLock(settings.home / "mini_runtime.lock", timeout=0)
        try:
            self.lease.acquire()
        except Timeout:
            raise ValueError("该数据目录已有助手运行。请使用现有网页，或通过 --home 选择独立数据目录。") from None
        self.provider = provider or ModelProvider(settings)
        self.store = SessionStore(settings.home)
        self.slots = asyncio.Semaphore(settings.max_subagents)
        self.write_locks: dict[Path, asyncio.Lock] = {}
        self.active: dict[str, asyncio.Task[None]] = {}
        self.outputs: dict[str, EventSink] = {}

    def create_session(self, workspace: str):
        root = project_path(workspace)
        if self.settings.home.is_relative_to(root):
            raise ValueError("代码目录不能包含配置和聊天数据目录，请选择更具体的项目。")
        return self.store.create(root)

    def snapshot(self, identity: str):
        item = self.store.get(identity)
        if item["validation"].get("status") == "passed":
            item["validation"] = ValidationRunner(Path(item["workspace"]), self.settings).refresh(item["validation"])
            self.store.save_validation(identity, item["validation"])
        return {**item, "messages": visible_messages(item["messages"]), "events": self.store.events(identity)}

    #检查会话是否已有任务运行，创建异步任务。
    def start(self, identity: str, message: str, emit: EventSink = discard_event) -> asyncio.Task[None]:
        session = self.store.get(identity)
        if identity in self.active:
            raise ValueError("该会话正在执行，请先停止或等待完成。")
        if not isinstance(message, str) or not message.strip() or len(message) > 20000:
            raise ValueError("消息应为 1～20000 个字符。")
        with event_scope(run_id=uuid.uuid4().hex, session_id=identity, task_id="main", role="main",
                         parent_task_id=None, parent_span_id=None, purpose="generation", workflow_attempt=1):
            task = asyncio.create_task(self.perform(session, message, emit))
        self.active[identity] = task
        self.outputs[identity] = emit
        return task

    #读取历史，加入本次用户消息，组装系统提示词和工具集。
    async def perform(self, session, message: str, output: EventSink) -> None:
        output = traced_sink(output)
        identity = session["id"]
        workspace = Path(session["workspace"])
        history = repair_history(session["messages"])
        history.append({"role": "user", "content": message})
        summary = session["summary"]
        manager: SubagentManager | None = None
        budget = BudgetedProvider(self.provider, self.settings.max_model_calls)
        runner = AgentRunner(budget, self.settings)
        validator = ValidationRunner(workspace, self.settings)
        quota = TaskQuota()

        async def emit(event):
            event = redact(event, self.settings.api_key)
            if event["type"] in {"tool", "task", "error", "compaction", "model", "validation", "repair", "report_repair"}:
                event = self.store.record(identity, event)
            await output(event)

        emit = traced_sink(emit)

        async def checkpoint(messages, current_summary):
            nonlocal history, summary
            history, summary = messages, current_summary
            self.store.save(identity, history, summary, "running")
            await output({"type": "history", "messages": visible_messages(history)})

        status = "idle"
        try:
            workspace = project_path(str(workspace))
            validator.paths(list(validator.rules))
            await checkpoint(history, summary)
            system = build_system(workspace, self.store.read_memory(workspace))
            if validator.rules:
                system += "\n可信后端验收 ID：" + "、".join(validator.rules) + "。实现计划可用 validation 引用这些 ID；后端会在最终回答后执行全部验收，失败时反馈并限次修复。"
            lock = self.write_locks.setdefault(workspace, asyncio.Lock())

            async def execute(attempt, feedback):
                nonlocal manager
                if manager:
                    await manager.close()
                self.store.save_validation(identity, {"status": "pending", "attempt": attempt, "checks": []})
                if feedback:
                    history.append({"role": "user", "content": feedback})
                manager = SubagentManager(runner, system,
                    lambda role: build_tools(workspace, self.settings, self.store, lock, role), emit, self.slots,
                    validator=validator, report_repairs=self.settings.report_repairs, quota=quota)
                tools = build_tools(workspace, self.settings, self.store, lock, subagent=manager.dispatch)

                async def settle():
                    return await manager.notifications(wait=True)

                await runner.run(system, history, tools, emit, checkpoint, summary,
                                 inject=manager.notifications, settle=settle)
                await manager.close()
                return not budget.exhausted

            async def validate(attempt):
                return await validator.run(emit=emit, attempt=attempt)

            async def save_validation(result):
                self.store.save_validation(identity, result)

            async with asyncio.timeout(self.settings.run_timeout):
                attempts = await validation_loop(execute, validate, emit, self.settings.max_repairs, save_validation)
            if attempts[-1]["status"] not in {"passed", "not_configured"}:
                status = "needs_followup"
                notice = "后端验收未通过，任务仍需处理。" + attempts[-1].get("reason", "已达到自动修复上限。")
                history.append({"role": "assistant", "content": notice})
                await emit({"type": "error", "message": notice})
        except asyncio.CancelledError:
            status = "cancelled"
            await emit({"type": "error", "message": "任务已停止；命令进程组和子任务会一并清理。"})
        except Exception as error:
            status = "error"
            await emit({"type": "error", "message": str(error) or "请求达到总执行时间上限。"})
        finally:
            if manager:
                await manager.close()
            validation = self.store.get(identity)["validation"]
            if validation.get("status") in {"pending", "running"}:
                self.store.save_validation(identity, {**validation, "status": "not_run", "reason": "执行中断，当前产物未完成后端验收。"})
            self.store.save(identity, history, summary, status)
            self.active.pop(identity, None)
            await output({"type": "done", "session": self.snapshot(identity)})
            self.outputs.pop(identity, None)

    async def stop(self, identity: str) -> None:
        if task := self.active.get(identity):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # 极快的停止请求可能在协程开始前到达，此时 perform 的 finally 尚未进入。
            if self.active.pop(identity, None) is not None:
                item = self.store.get(identity)
                self.store.save(identity, item["messages"], item["summary"], "cancelled")
                await self.outputs.pop(identity)({"type": "done", "session": self.snapshot(identity)})

    async def close(self) -> None:
        for identity in list(self.active):
            await self.stop(identity)
        await self.provider.close()
        self.store.close()
        self.lease.release()
