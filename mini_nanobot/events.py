"""前端与终端共用的运行事件；核心执行不依赖任何页面组件。"""

from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

Event = dict[str, Any]
EventSink = Callable[[Event], Awaitable[None]]
TRACE_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("mini_trace", default={})


@contextmanager
def event_scope(**fields: Any):
    """上下文随 asyncio Task 传播；并发角色不会串用任务归属。"""
    token = TRACE_CONTEXT.set({**TRACE_CONTEXT.get(), **fields})
    try:
        yield
    finally:
        TRACE_CONTEXT.reset(token)


def traced_sink(sink: EventSink) -> EventSink:
    async def emit(event: Event) -> None:
        await sink({**TRACE_CONTEXT.get(), "timestamp": datetime.now(timezone.utc).isoformat(), **event})
    return emit


def redact(value: Any, secret: str) -> Any:
    """事件和后端验收只保存脱敏副本，不改写执行参数。"""
    if isinstance(value, str):
        return value.replace(secret, "[密钥已隐藏]") if secret else value
    if isinstance(value, dict):
        return {key: redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    return value


async def discard_event(event: Event) -> None:
    """无界面执行时忽略流式事件。"""
