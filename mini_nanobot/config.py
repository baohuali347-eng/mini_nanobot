"""配置入口：读取 .env，并兼容已有 nanobot 配置中的核心字段。"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


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


def load_settings(home: Path | None = None, config_path: Path | None = None) -> Settings:
    load_dotenv(Path.cwd() / ".env", override=False)
    home = (home or Path("~/.mini_nanobot")).expanduser().resolve()
    path = config_path.expanduser().resolve() if config_path else home / "config.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    defaults = data.get("agents", {}).get("defaults", {})
    connection = data.get("providers", {}).get(defaults.get("provider", "custom"), {})
    execution = data.get("tools", {}).get("exec", {})

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
    return settings
