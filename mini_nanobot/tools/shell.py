"""命令执行：工作目录检查、基本命令过滤、可选系统沙箱与进程组清理。"""

import asyncio
import os
import re
import shlex
import signal
import sys
from contextlib import suppress
from typing import Any

from mini_nanobot.config import Settings
from mini_nanobot.security import resolve_path
from mini_nanobot.tools.base import Tool, ToolResult, parameters, text


class ExecuteCommand(Tool):
    name = "exec"
    description = "在当前项目执行命令并返回退出码和输出；停止任务会终止命令进程组。"
    parameters = parameters(["command"], command=text("命令"), working_dir=text("项目内的执行目录，默认当前项目"))

    def __init__(self, workspace, settings: Settings):
        self.workspace = workspace
        self.settings = settings

    async def execute(self, command: str, working_dir: str = ".", **kwargs: Any) -> str:
        original_command = command
        cwd = resolve_path(self.workspace, working_dir)
        if not cwd.is_dir():
            raise ValueError("命令工作目录不存在。")
        patterns = [r"\brm\s+.*-[a-z]*[rf]", r"\b(sudo|mkfs|shutdown|reboot)\b", r"\bdd\s+", r"git\s+(reset\s+--hard|clean\s+-[a-z]*f)", r"\.env\b", r"\.git/", r"\.\./", r"~/"]
        if any(re.search(pattern, command, re.I) for pattern in patterns):
            raise PermissionError("命令包含危险操作、密钥文件或明显的目录越界。")
        # 只能识别显式路径；真正的进程级隔离由可选沙箱完成。
        for index, token in enumerate(shlex.split(command, posix=sys.platform != "win32")):
            if token.startswith("/") and index != 0 and token not in {"/dev/null"}:
                resolve_path(self.workspace, token)
        if self.settings.sandbox:
            if sys.platform == "win32":
                raise ValueError("当前沙箱后端不支持 Windows，未执行命令。")
            from mini_nanobot.sandbox import wrap_command
            command = wrap_command(
                self.settings.sandbox, command, str(self.workspace), str(cwd),
                sandbox_ro_binds=self.settings.sandbox_read,
                sandbox_rw_binds=self.settings.sandbox_write,
            )
        environment = {key: value for key, value in os.environ.items() if key in {
            "PATH", "LANG", "LC_ALL", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMPDIR",
        }}
        process = await asyncio.create_subprocess_shell(
            command, cwd=cwd, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=sys.platform != "win32",
        )
        output = bytearray()

        async def drain() -> None:
            assert process.stdout is not None
            while chunk := await process.stdout.read(4096):
                if len(output) < 20000:
                    output.extend(chunk[:20000 - len(output)])
            await process.wait()

        async def terminate() -> None:
            with suppress(ProcessLookupError):
                if sys.platform == "win32":
                    killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F")
                    await killer.wait()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            await process.communicate()

        try:
            await asyncio.wait_for(drain(), self.settings.command_timeout)
        except asyncio.TimeoutError:
            await terminate()
            return ToolResult("命令超时，已终止进程组。\n" + output.decode(errors="replace"), ok=False, evidence={
                "kind": "command", "command": original_command, "working_dir": str(cwd.relative_to(self.workspace)),
                "exit_code": process.returncode, "timed_out": True, "output": output.decode(errors="replace"),
            })
        except asyncio.CancelledError:
            await terminate()
            raise
        return ToolResult(f"退出码：{process.returncode}\n{output.decode(errors='replace')}", ok=process.returncode == 0, evidence={
            "kind": "command", "command": original_command, "working_dir": str(cwd.relative_to(self.workspace)),
            "exit_code": process.returncode, "timed_out": False, "output": output.decode(errors="replace"),
        })
