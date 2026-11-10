"""检查精简架构：核心不能导入服务/界面，项目不能残留旧框架依赖。"""
import ast
from pathlib import Path

root = Path(__file__).resolve().parent.parent
core = {'agent', 'runner', 'context', 'subagents', 'task_graph', 'provider', 'session', 'tools', 'security', 'sandbox'}
errors = []
for file in (root / 'mini_nanobot').rglob('*.py'):
    for node in ast.walk(ast.parse(file.read_text())):
        imports = [name.name for name in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
        for name in imports:
            relative = file.relative_to(root / 'mini_nanobot')
            if name == 'nanobot' or name.startswith('nanobot.'):
                errors.append(f'{relative}:{node.lineno} 仍然依赖旧框架 {name}')
            if relative.parts[0].split('.')[0] in core and name in {'mini_nanobot.cli', 'mini_nanobot.server'}:
                errors.append(f'{relative}:{node.lineno} 核心不应导入服务入口 {name}')
if errors:
    raise SystemExit('\n'.join(errors))
print('架构边界通过：核心独立，未残留旧框架导入。')
