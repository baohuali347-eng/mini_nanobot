"""唯一的模型适配器：OpenAI 兼容的流式 Chat Completions 协议。"""

import asyncio
import json
import time
import uuid
from typing import Any

import httpx

from mini_nanobot.config import Settings
from mini_nanobot.events import EventSink, discard_event, traced_sink

Message = dict[str, Any]


class BudgetExceeded(RuntimeError):
    pass


class BudgetedProvider:
    """一次请求内，所有角色、摘要、格式更正和业务修复共享额度。"""

    def __init__(self, provider, limit: int):
        self.provider, self.limit = provider, limit
        self.calls = self.requests = 0
        self.exhausted = False

    async def complete(self, messages, tools=None, emit: EventSink = discard_event):
        if self.calls >= self.limit:
            self.exhausted = True
            raise BudgetExceeded("已达到本次请求的总模型调用预算。")
        self.calls += 1

        async def forward(event):
            if event["type"] == "model" and event["state"] == "running":
                if self.requests >= self.limit:
                    self.exhausted = True
                    raise BudgetExceeded("已达到总 HTTP 模型请求预算（包含重试）。")
                self.requests += 1
            await emit(event)

        return await self.provider.complete(messages, tools, forward)


class ModelProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15))

    async def close(self) -> None:
        await self.client.aclose()

    async def complete(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None,
        emit: EventSink = discard_event,
    ) -> Message:
        if not self.settings.api_key:
            raise ValueError("尚未配置 DeepSeek 密钥，请填写 .env 后重新启动。")
        emit = traced_sink(emit)
        span_id = uuid.uuid4().hex
        payload: dict[str, Any] = {
            "model": self.settings.model, "messages": messages,
            "stream": True, "max_tokens": 4096,
        }
        if tools:
            payload["tools"] = tools
        if self.settings.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        url = self.settings.api_base.rstrip("/") + "/chat/completions"
        for attempt in range(3):
            request_id = uuid.uuid4().hex
            trace = {"type": "model", "span_id": span_id, "request_id": request_id,
                     "attempt": attempt + 1, "model": self.settings.model}
            started = time.perf_counter()
            usage = None
            first_token_ms = None
            await emit({**trace, "state": "running"})
            emitted = False
            content, reasoning, finish = "", "", None
            calls: dict[int, dict[str, Any]] = {}
            try:
                async with self.client.stream(
                    "POST", url, json=payload,
                    headers={"Authorization": f"Bearer {self.settings.api_key}"},
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                            continue
                        packet = json.loads(line[5:].strip())
                        # usage 常位于 choices=[] 的独立末包，必须先读取。
                        if isinstance(packet.get("usage"), dict):
                            raw = packet["usage"]
                            usage = {key: raw.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
                            usage = {key: value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
                                     for key, value in usage.items()}
                        if not packet.get("choices"):
                            continue
                        choice = packet["choices"][0]
                        finish = choice.get("finish_reason") or finish
                        delta = choice.get("delta", {})
                        if first_token_ms is None and any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls")):
                            first_token_ms = round((time.perf_counter() - started) * 1000, 3)
                        reasoning += delta.get("reasoning_content") or ""
                        if text := delta.get("content"):
                            content += text
                            emitted = True
                            await emit({"type": "text", "text": text, "span_id": span_id, "request_id": request_id})
                        for part in delta.get("tool_calls") or []:
                            emitted = True
                            entry = calls.setdefault(part["index"], {
                                "id": "", "type": "function", "function": {"name": "", "arguments": ""},
                            })
                            entry["id"] += part.get("id") or ""
                            function = part.get("function", {})
                            entry["function"]["name"] += function.get("name") or ""
                            entry["function"]["arguments"] += function.get("arguments") or ""
                if finish is None:
                    raise ValueError("模型连接提前结束，请重试；已有工具结果仍保留在会话中。")
                answer: Message = {"role": "assistant", "content": content or None}
                # DeepSeek 思考模式要求后续工具轮次带回 reasoning_content。
                if reasoning:
                    answer["reasoning_content"] = reasoning
                if calls:
                    answer["tool_calls"] = [calls[index] for index in sorted(calls)]
                answer["_finish_reason"] = finish
                answer["_span_id"] = span_id
                await emit({**trace, "state": "done", "finish_reason": finish,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                            "first_token_ms": first_token_ms, "usage": usage,
                            "usage_source": "provider" if usage and usage.get("total_tokens") is not None else "unavailable"})
                return answer
            except asyncio.CancelledError:
                await emit({**trace, "state": "cancelled", "usage": usage,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
                raise
            except (httpx.HTTPError, ValueError) as error:
                transient = isinstance(error, httpx.TransportError) or (
                    isinstance(error, httpx.HTTPStatusError)
                    and error.response.status_code in {429, 500, 502, 503, 504}
                )
                retry = not emitted and transient and attempt < 2
                await emit({**trace, "state": "error", "retrying": retry, "usage": usage,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                            "error_type": type(error).__name__})
                if retry:
                    await asyncio.sleep(2 ** attempt)
                    continue
                detail = str(error).replace(self.settings.api_key, "[密钥已隐藏]")
                raise RuntimeError(f"模型调用失败：{detail}") from None
        raise RuntimeError("模型暂时不可用。")
