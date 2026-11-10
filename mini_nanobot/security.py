"""工作目录边界：解析符号链接后检查；文件权限与 Shell 隔离分别负责。"""

from pathlib import Path

IGNORED = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}


def project_path(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("工作目录不存在，或不是文件夹。")
    if path == Path(path.anchor) or path == Path.home():
        raise ValueError("请选择具体的代码项目目录，不能使用磁盘根目录或整个用户目录。")
    return path


def resolve_path(workspace: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    path = (candidate if candidate.is_absolute() else workspace / candidate).resolve()
    if not path.is_relative_to(workspace):
        raise PermissionError("路径超出当前代码目录，请勿通过其他工具绕过。")
    relative = path.relative_to(workspace)
    if any(part == ".git" or part == ".env" or part.startswith(".env.") for part in relative.parts):
        raise PermissionError("工具不允许访问 Git 内部文件或环境密钥文件。")
    return path


def path_in_scope(relative: str, scopes: list[str]) -> bool:
    path = Path(relative)
    return any(scope == "." or path == Path(scope) or path.is_relative_to(Path(scope)) for scope in scopes)


def write_scopes(workspace: Path, paths: list[str] | None) -> list[str]:
    if paths is not None and not paths:
        raise ValueError("写入范围不能为空。")
    return sorted({str(resolve_path(workspace, raw).relative_to(workspace)) for raw in (paths or ["."])})
