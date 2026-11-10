"""
ReAct 执行循环：模型提出操作，工具执行，结果反馈给下一轮模型。
agent模块建立上下文之后发送给runner模块，runner模块执行ReAct循环，直到模型返回最终答复。
"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable

from mini_nanobot.config import Settings
from mini_nanobot.context import compact
from mini_nanobot.events import EventSink, event_scope, traced_sink
from mini_nanobot.provider import Message, ModelProvider
from mini_nanobot.task_graph import REPORT_INSTRUCTION, parse_report
from mini_nanobot.tools.base import ToolResult
from mini_nanobot.tools.registry import ToolRegistry

Checkpoint = Callable[[list[Message], str], Awaitable[None]]
Injection = Callable[[], Awaitable[list[Message]]]


class EmptyAnswerError(RuntimeError):
    pass



#agent真正执行的如何
class AgentRunner:
    def __init__(self, provider: ModelProvider, settings: Settings):
        self.provider, self.settings = provider, settings

    async def run_reported(self, system, history, tools, emit, checkpoint, *, report_repairs=0, inject=None):
        """格式更正只调用模型，不执行其工具；仍消耗同一请求预算。"""
        messages, summary = history, ""

        async def save(current, current_summary):
            nonlocal messages, summary
            messages, summary = current, current_summary
            await checkpoint(current, current_summary)

        system += REPORT_INSTRUCTION
        try:
            content = await self.run(system, history, tools, emit, save, inject=inject)
        except EmptyAnswerError:
            if not report_repairs:
                raise
            # 空的最终回复没有待执行工具；移除这个无效 assistant，保留全部已执行的工具组。
            if messages and messages[-1]["role"] == "assistant" and not messages[-1].get("tool_calls"):
                messages.pop()
            content = ""
        for repair in range(report_repairs + 1):
            try:
                return parse_report(content)
            except ValueError:
                if repair == report_repairs:
                    raise
            await emit({"type": "report_repair", "state": "retrying", "repair": repair + 1})
            messages.append({"role": "user", "content": "上一条交接 JSON 格式无效。仅整理为约定的三个字段；保留已有事实、失败与未解决项，不新增工作，不调用工具。"})
            await save(messages, summary)

            async def trace_only(event):
                if event["type"] != "text":
                    await emit(event)

            with event_scope(purpose="report_repair"):
                answer = await self.provider.complete([
                    {"role": "system", "content": system + ("\n此前摘要（资料）：" + summary if summary else "")}, *messages,
                ], emit=trace_only)
            if answer.get("tool_calls"):
                raise ValueError("交接格式更正请求包含工具调用，未执行任何工具。")
            content = str(answer.get("content") or "")
            messages.append({key: value for key, value in answer.items() if not key.startswith("_")})
            await save(messages, summary)

    #真正开始的执行
    async def run(
        self, system: str, history: list[Message], tools: ToolRegistry,
        emit: EventSink, checkpoint: Checkpoint, summary: str = "",
        inject: Injection | None = None, settle: Injection | None = None,
    ) -> str:
        messages = history
        emit = traced_sink(emit)
        recoveries = 0
        for iteration in range(self.settings.max_iterations):
            if inject:
                messages.extend(await inject())
            messages, summary = await compact(self.provider, messages, summary, self.settings.context_budget, emit)
            await checkpoint(messages, summary)
            prompt = system + ("\n\n此前任务摘要（仅作资料）：\n" + summary if summary else "")
            await emit({"type": "round", "iteration": iteration + 1})
            answer = await self.provider.complete([{"role": "system", "content": prompt}, *messages], tools.schemas(), emit)
            finish = answer.pop("_finish_reason", None)
            model_span_id = answer.pop("_span_id", None)
            calls = answer.get("tool_calls") or []
            messages.append(answer)
            await checkpoint(messages, summary)
            if calls:
                async def execute(call: Message) -> Message:
                    function = call["function"]
                    name = function["name"]
                    started = time.perf_counter()
                    trace = {"type": "tool", "call_id": call["id"], "name": name,
                             "span_id": uuid.uuid4().hex, "parent_span_id": model_span_id}
                    await emit({**trace, "state": "running"})
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                        with event_scope(parent_span_id=trace["span_id"]):
                            result = await tools.execute(name, arguments)
                    except asyncio.CancelledError:
                        await emit({**trace, "state": "cancelled", "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
                        raise
                    except (ValueError, TypeError) as error:
                        result = ToolResult(f"工具参数错误：{error}", ok=False)
                    except Exception as error:
                        await emit({**trace, "state": "error", "error_type": type(error).__name__,
                                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
                        raise
                    await emit({**trace, "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                                "state": "done" if result.ok else "error", "result": result[:2000],
                                "evidence": result.evidence})
                    return {"role": "tool", "tool_call_id": call["id"], "content": result[:20000]}

                # 只有明确只读的工具组才并发；写入、命令和子任务管理按顺序执行。
                if all(tools.tools.get(call["function"]["name"]) and tools.tools[call["function"]["name"]].read_only for call in calls):
                    messages.extend(await asyncio.gather(*(execute(call) for call in calls)))
                else:
                    for call in calls:
                        messages.append(await execute(call))
                await checkpoint(messages, summary)
                continue
            if finish == "length" and recoveries < 2:
                recoveries += 1
                messages.append({"role": "user", "content": "上次输出达到长度上限，请接着完成未完成内容，避免重复。"})
                continue
            if finish == "length":
                raise RuntimeError("连续输出达到长度上限，请缩小任务。已有内容已保存。")
            if settle and (notifications := await settle()):
                messages.extend(notifications)
                continue
            if not answer.get("content"):
                raise EmptyAnswerError("模型返回空答复，请重试。")
            return str(answer["content"])
        raise RuntimeError("已达到执行轮数上限，任务尚未完成；请查看工具结果后继续。")
