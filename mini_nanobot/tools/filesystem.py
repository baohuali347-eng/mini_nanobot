"""文件工具：读取、列目录、搜索、写入和精确替换。搜索不启动外部程序。"""

import asyncio
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from mini_nanobot.security import IGNORED, resolve_path
from mini_nanobot.tools.base import Tool, ToolResult, parameters, text


class FileTool(Tool):
    def __init__(self, workspace: Path, lock: asyncio.Lock):
        self.workspace = workspace
        self.lock = lock

    def path(self, raw: str) -> Path:
        return resolve_path(self.workspace, raw)

    def atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mini-write-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


class ReadFile(FileTool):
    name = "read_file"
    description = "读取文本文件，返回行号；大文件请分页读取。"
    read_only = True
    parameters = parameters(["path"], path=text("文件路径"), start_line={"type": "integer", "minimum": 1}, end_line={"type": "integer", "minimum": 1})

    async def execute(self, path: str, start_line: int = 1, end_line: int = 300, **kwargs: Any) -> str:
        target = self.path(path)
        if target.stat().st_size > 2_000_000:
            raise ValueError("文件大于 2 MB，请先搜索相关内容。")
        lines = target.read_text(encoding="utf-8").splitlines()
        end = min(end_line, start_line + 499)
        return "\n".join(f"{index + 1}| {line}" for index, line in enumerate(lines) if start_line <= index + 1 <= end)[:20000]


class ListDirectory(FileTool):
    name = "list_dir"
    description = "列出目录中的文件；默认跳过依赖和构建目录。"
    read_only = True
    parameters = parameters(path=text("目录路径，默认为当前目录"))

    async def execute(self, path: str = ".", **kwargs: Any) -> str:
        entries = []
        for entry in sorted(self.path(path).iterdir()):
            if entry.name in IGNORED or entry.name.startswith(".env"):
                continue
            try:
                self.path(str(entry))
            except PermissionError:
                continue
            entries.append(entry.name + ("/" if entry.is_dir() else ""))
        return "\n".join(entries[:300]) or "目录为空。"


class SearchFiles(FileTool):
    name = "search"
    description = "在项目文本文件中搜索关键词，返回路径、行号和匹配内容。"
    read_only = True
    parameters = parameters(["query"], query=text("要搜索的文本"), path=text("搜索目录，默认为当前目录"))

    async def execute(self, query: str, path: str = ".", **kwargs: Any) -> str:
        if not query:
            raise ValueError("搜索内容不能为空。")
        root = self.path(path)

        def scan() -> str:
            matches, visited = [], 0
            for directory, directories, files in os.walk(root, followlinks=False):
                directories[:] = [name for name in directories if name not in IGNORED]
                for name in files:
                    visited += 1
                    if visited > 10000:
                        return "\n".join(matches) + "\n扫描达到 10000 个文件上限，请缩小目录。"
                    try:
                        candidate = self.path(str(Path(directory) / name))
                        if candidate.stat().st_size > 500000:
                            continue
                        for number, line in enumerate(candidate.read_text(encoding="utf-8").splitlines(), 1):
                            if query.casefold() in line.casefold():
                                matches.append(f"{candidate.relative_to(self.workspace)}:{number}: {line[:250]}")
                                if len(matches) >= 80:
                                    return "\n".join(matches) + "\n已达到 80 条结果上限。"
                    except (OSError, UnicodeError):
                        continue
            return "\n".join(matches) or "没有找到匹配内容。"
        return await asyncio.to_thread(scan)


class WriteFile(FileTool):
    name = "write_file"
    description = "写入完整文本文件；修改现有文件前先读取其内容。"
    parameters = parameters(["path", "content"], path=text("文件路径"), content=text("完整文本"))

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        async with self.lock:
            target = self.path(path)
            self.atomic_write(target, content)
        return ToolResult(f"已写入：{path}", evidence={"kind": "file_change", "path": str(target.relative_to(self.workspace))})


class EditFile(FileTool):
    name = "edit_file"
    description = "精确替换文件中的唯一一段文本；旧文本必须与读取结果一致。"
    parameters = parameters(["path", "old", "new"], path=text("文件路径"), old=text("需要替换的原始文本"), new=text("新文本"))

    async def execute(self, path: str, old: str, new: str, **kwargs: Any) -> str:
        if not old:
            raise ValueError("原始文本不能为空。")
        async with self.lock:
            target = self.path(path)
            content = target.read_text(encoding="utf-8")
            if content.count(old) != 1:
                raise ValueError("原始文本必须恰好出现一次，请重新读取文件定位。")
            self.atomic_write(target, content.replace(old, new, 1))
        return ToolResult(f"已修改：{path}", evidence={"kind": "file_change", "path": str(target.relative_to(self.workspace))})
