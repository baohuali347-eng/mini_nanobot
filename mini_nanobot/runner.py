"""
ReAct 执行循环：模型提出操作，工具执行，结果反馈给下一轮模型。
agent模块建立上下文之后发送给runner模块，runner模块执行ReAct循环，直到模型返回最终答复。

逻辑流程：
for 每一轮 in 最大执行轮数:
    接收子任务通知()
    必要时压缩上下文()
    保存检查点()

    回复 = await 调用模型(上下文, 工具说明)
    保存模型回复()

    if 回复包含工具调用:
        执行工具()
        把工具结果加入上下文()
        continue

    if 还有子任务结果需要汇总:
        把子任务结果加入上下文()
        continue

    return 最终回复

"""

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable

from mini_nanobot.config import Settings
from mini_nanobot.context import compact
from mini_nanobot.events import EventSink, event_scope, traced_sink
from mini_nanobot.provider import BudgetedProvider, Message, ModelProvider
from mini_nanobot.task_graph import REPORT_INSTRUCTION, parse_report
from mini_nanobot.tools.base import ToolResult
from mini_nanobot.tools.registry import ToolRegistry

Checkpoint = Callable[[list[Message], str], Awaitable[None]]
Injection = Callable[[], Awaitable[list[Message]]]


class EmptyAnswerError(RuntimeError):
    pass



#agent真正执行的如何
class AgentRunner:
    #初始化
    def __init__(self, provider: ModelProvider, settings: Settings):
        self.provider, self.settings = provider, settings

    # 调用 run()，再把结果校验、整理成结构化交接（这个应该是用在子agent上面）
    async def run_reported(self, system, history, tools, emit, checkpoint, *, report_repairs=0, inject=None, settle=None):
        messages, summary = history, ""

        async def save(current, current_summary):
            nonlocal messages, summary
            messages, summary = current, current_summary
            await checkpoint(current, current_summary)

        system += REPORT_INSTRUCTION
        try:
            content = await self.run(system, history, tools, emit, save, inject=inject, settle=settle)
        except EmptyAnswerError:
            if not report_repairs:
                raise
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
            if content:
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
        #开始执行每一轮
        for iteration in range(self.settings.max_iterations):
            if inject:
                messages.extend(await inject())
            #compact位于context里面，对上下文进行压缩，以及总结上下文。
            messages, summary = await compact(self.provider, messages, summary, self.settings.context_budget, emit)
            await checkpoint(messages, summary)
            prompt = system + ("\n\n此前任务摘要（仅作资料）：\n" + summary if summary else "")
            if isinstance(self.provider, BudgetedProvider):
                prompt += (f"\n当前共享模型请求额度剩余 {self.provider.remaining_calls} 次，包含本轮，"
                           "主任务、子任务、摘要和重试共同使用。请为最终汇报保留额度；"
                           "额度不足时优先核对已有证据，已验证完成则汇报，未验证或未完成则如实列出。")
            await emit({"type": "round", "iteration": iteration + 1})
            #provider.complete()是调用模型的接口，传入上下文和工具说明，返回模型的答复。
            answer = await self.provider.complete([{"role": "system", "content": prompt}, *messages], tools.schemas(), emit)
            #answer是模型的答复，可能包含工具调用信息。这里从answer中提取_finish_reason和_span_id，并将工具调用信息存储在calls中。
            finish = answer.pop("_finish_reason", None)
            model_span_id = answer.pop("_span_id", None)
            #这里面是调用工具的名称，提取出来
            calls = answer.get("tool_calls") or []
            if finish == "length" and (calls or not answer.get("content")):
                raise RuntimeError(
                    f"模型达到单次输出上限（{self.settings.max_output_tokens} Token），当前响应不完整，已停止本轮。"
                    "已有工具结果已保存，请缩小任务或在配置中调整 maxOutputTokens 后重试。"
                )
            messages.append(answer)
            #保存检查点，messages是当前的上下文，summary是当前的摘要
            await checkpoint(messages, summary)
            if calls:
                #execute是一个异步函数，用于执行工具调用。它接收一个工具调用消息，提取工具名称和参数，执行工具，并返回工具的结果。 
                #这是在这里面定义了一个execute函数，就在这里面执行
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
                #并行执行这些工具
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
