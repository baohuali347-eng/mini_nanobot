"""工具协议：统一参数说明、校验、执行和错误返回。"""

from abc import ABC, abstractmethod
from typing import Any

from jsonschema import ValidationError, validate


class ToolResult(str):
    """保持模型文本协议，同时携带后端产生的执行证据。"""

    def __new__(cls, content: str, *, ok: bool = True, evidence: dict[str, Any] | None = None):
        result = super().__new__(cls, content)
        result.ok, result.evidence = ok, evidence
        return result


class Tool(ABC):
    name: str
    description: str
    read_only = False
    parameters: dict[str, Any]

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}

    async def call(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            validate(arguments, self.parameters)
            result = await self.execute(**arguments)
            return result if isinstance(result, ToolResult) else ToolResult(result)
        except (ValidationError, ValueError, OSError) as error:
            return ToolResult(f"工具错误：{error}", ok=False)

    @abstractmethod
    async def execute(self, **arguments: Any) -> str: ...


def parameters(required: list[str] | None = None, **properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def text(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}
