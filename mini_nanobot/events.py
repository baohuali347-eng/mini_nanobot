"""前端与终端共用的运行事件；核心执行不依赖任何页面组件。"""

from collections.abc import Awaitable, Callable
from typing import Any

Event = dict[str, Any]
EventSink = Callable[[Event], Awaitable[None]]


async def discard_event(event: Event) -> None:
    """无界面执行时忽略流式事件。"""
