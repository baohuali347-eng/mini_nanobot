"""配置入口：读取 .env，并兼容已有 nanobot 配置中的核心字段。"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from jsonschema import ValidationError, validate


@dataclass(frozen=True)
class ValidationRule:
    id: str
    command: str
    paths: list[str] = field(default_factory=lambda: ["."])
    working_dir: str = "."
    timeout: int = 120


@dataclass
class Settings:
    home: Path
    model: str = "deepseek-flash"
    api_base: str = "https://api.deepseek.com"
    api_key: str = field(default="", repr=False)
    workspace: Path = field(default_factory=Path.cwd)
    max_iterations: int = 40
    max_subagents: int = 4
    context_budget: int = 24000
    command_timeout: int = 120
    sandbox: str = ""
    sandbox_read: list[str] = field(default_factory=list)
    sandbox_write: list[str] = field(default_factory=list)
    stream_usage: bool = True
    max_model_calls: int = 60
    run_timeout: int = 600
    max_repairs: int = 2
    report_repairs: int = 1
    validation_rules: list[ValidationRule] = field(default_factory=list)
    isolate_subagents: bool = True
    allow_subagent_deletes: bool = False
    max_output_tokens: int = 8192


def load_settings(home: Path | None = None, config_path: Path | None = None) -> Settings:
    load_dotenv(Path.cwd() / ".env", override=False)
    home = (home or Path("~/.mini_nanobot")).expanduser().resolve()
    path = config_path.expanduser().resolve() if config_path else home / "config.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    defaults = data.get("agents", {}).get("defaults", {})
    connection = data.get("providers", {}).get(defaults.get("provider", "custom"), {})
    execution = data.get("tools", {}).get("exec", {})
    validation = data.get("validation", {})
    try:
        validate(validation, {"type": "object", "additionalProperties": False, "properties": {
            "maxRepairAttempts": {"type": "integer", "minimum": 0, "maximum": 2},
            "reportRepairAttempts": {"type": "integer", "minimum": 0, "maximum": 2},
            "commands": {"type": "array", "maxItems": 6, "items": {
                "type": "object", "additionalProperties": False, "required": ["id", "command"],
                "properties": {"id": {"type": "string", "pattern": "^[a-zA-Z][a-zA-Z0-9_-]{0,39}$"},
                               "command": {"type": "string", "minLength": 1, "maxLength": 12000},
                               "paths": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"type": "string", "minLength": 1}},
                               "workingDir": {"type": "string", "minLength": 1},
                               "timeout": {"type": "integer", "minimum": 1, "maximum": 3600}},
            }}}})
    except ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path) or "validation"
        raise ValueError(f"后端验收配置无效：{location} 不符合 {error.validator} 约束。") from error
    rules = [ValidationRule(row["id"], row["command"], row.get("paths", ["."]), row.get("workingDir", "."), row.get("timeout", 120))
             for row in validation.get("commands", [])]
    if len({rule.id for rule in rules}) != len(rules) or any(not rule.command.strip() for rule in rules):
        raise ValueError("验收 ID 必须唯一，命令不能为空。")

    def resolve(value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ValueError(f"缺少环境变量 {name}，请在项目 .env 中填写。")
            return os.environ[name]
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, str(value))

    settings = Settings(
        home=home,
        model=resolve(defaults.get("model", os.getenv("MINI_NANOBOT_MODEL", "deepseek-flash"))),
        api_base=resolve(connection.get("apiBase") or os.getenv("MINI_NANOBOT_API_BASE", "https://api.deepseek.com")),
        api_key=resolve(connection.get("apiKey") or os.getenv("MINI_NANOBOT_API_KEY", "")),
        workspace=Path(resolve(defaults.get("workspace", str(Path.cwd())))).expanduser().resolve(),
        max_iterations=int(defaults.get("maxToolIterations", 40)),
        max_subagents=int(defaults.get("maxConcurrentSubagents", 4)),
        context_budget=int(data.get("contextBudget", 24000)),
        command_timeout=int(execution.get("timeout", execution.get("maxRuntime", 120))),
        sandbox=execution.get("sandbox", ""),
        sandbox_read=execution.get("sandboxRoBinds", []),
        sandbox_write=execution.get("sandboxRwBinds", []),
        stream_usage=data.get("streamUsage", True),
        max_model_calls=int(data.get("maxModelCalls", 60)),
        run_timeout=int(data.get("runTimeout", 600)),
        max_repairs=validation.get("maxRepairAttempts", 2),
        report_repairs=validation.get("reportRepairAttempts", 1),
        validation_rules=rules,
        isolate_subagents=data.get("isolateSubagents", True),
        allow_subagent_deletes=data.get("allowSubagentDeletes", False),
        max_output_tokens=data.get("maxOutputTokens", 8192),
    )
    address = urlsplit(settings.api_base)
    if address.scheme not in {"http", "https"} or not address.netloc or address.username or address.query or address.fragment:
        raise ValueError("模型接口地址必须是有效的 HTTP(S) 地址。")
    if not 1 <= settings.max_iterations <= 200 or not 1 <= settings.max_subagents <= 16:
        raise ValueError("运行轮数应为 1～200，子任务并发数应为 1～16。")
    if not 500 <= settings.context_budget <= 200000 or not 1 <= settings.command_timeout <= 3600:
        raise ValueError("上下文预算应为 500～200000，命令超时应为 1～3600 秒。")
    if settings.sandbox not in {"", "seatbelt", "bwrap"}:
        raise ValueError("沙箱仅支持 seatbelt 或 bwrap。")
    if not isinstance(settings.stream_usage, bool):
        raise ValueError("streamUsage 应为布尔值。")
    if type(settings.max_output_tokens) is not int or not 1 <= settings.max_output_tokens <= 65536:
        raise ValueError("maxOutputTokens 应为 1～65536 的整数。")
    if not isinstance(settings.isolate_subagents, bool):
        raise ValueError("isolateSubagents 应为布尔值。")
    if not isinstance(settings.allow_subagent_deletes, bool):
        raise ValueError("allowSubagentDeletes 应为布尔值。")
    if not 1 <= settings.max_model_calls <= 200 or not 1 <= settings.run_timeout <= 3600:
        raise ValueError("总模型调用上限应为 1～200，总执行秒数应为 1～3600。")
    if settings.api_key and any(settings.api_key in rule.command for rule in rules):
        raise ValueError("后端验收命令不得包含模型密钥。")
    return settings
