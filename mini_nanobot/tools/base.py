"""工具协议：统一参数说明、校验、执行和错误返回。"""

from abc import ABC, abstractmethod
from typing import Any

from jsonschema import ValidationError, validate


#带执行状态和证据的字符串。
class ToolResult(str):
    """保持模型文本协议，同时携带后端产生的执行证据。"""
    #参数中的 * 表示后面的参数必须通过名称传入
    def __new__(cls, content: str, *, ok: bool = True, evidence: dict[str, Any] | None = None):
        result = super().__new__(cls, content)
        result.ok, result.evidence = ok, evidence
        return result

#规定工具的统一结构
class Tool(ABC):
    name: str
    description: str
    read_only = False
    parameters: dict[str, Any]

    #把工具说明整理成模型接口要求的格式
    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}

    #工具调用的统一接口，参数校验、执行和错误处理
    async def call(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            #1.校验参数
            validate(arguments, self.parameters)
            #2.执行工具
            result = await self.execute(**arguments)
            #3.返回结果，如果不是ToolResult类型，就包装成ToolResult类型
            return result if isinstance(result, ToolResult) else ToolResult(result)
        except (ValidationError, ValueError, OSError) as error:
            return ToolResult(f"工具错误：{error}", ok=False)

    #@abstractmethod表示这是一个抽象方法，子类必须实现这个方法，必须要自己实现自己的execute
    @abstractmethod
    async def execute(self, **arguments: Any) -> str: ...


#最后两个函数，是生成参数说明的小助手


"""
parameters(["path"], path=text("文件路径"))
==
{
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "文件路径"
        }
    },
    "required": ["path"],
    "additionalProperties": False
}


"""
def parameters(required: list[str] | None = None, **properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def text(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}
