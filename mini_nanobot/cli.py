"""中文命令入口：初始化配置、启动完整网页、终端对话。"""

import asyncio
import json
import subprocess
import webbrowser
from pathlib import Path

import typer

from mini_nanobot import __version__
from mini_nanobot.agent import AgentService
from mini_nanobot.config import load_settings

app = typer.Typer(name="mini_nanobot", help="中文 AI 编程助手：聊天、工具、多 agent 和项目记忆。", no_args_is_help=True)


@app.callback()
def configure(ctx: typer.Context, home: Path = typer.Option(Path("~/.mini_nanobot"), help="配置和聊天数据目录")):
    ctx.obj = home.expanduser().resolve()


@app.command("init")
def initialize(ctx: typer.Context, if_missing: bool = typer.Option(False, "--if-missing", help="配置存在时跳过")):
    """创建配置文件；模型密钥只通过环境变量读取。"""
    path = ctx.obj / "config.json"
    if path.exists():
        if if_missing:
            typer.echo("使用已有配置；旧版数据库保持原样。")
            return
        raise typer.BadParameter("配置已存在，请直接编辑，或添加 --if-missing。")
    path.parent.mkdir(parents=True, exist_ok=True)
    config = {"agents": {"defaults": {"model": "${MINI_NANOBOT_MODEL}", "provider": "custom",
              "maxToolIterations": 40, "maxConcurrentSubagents": 4}},
              "providers": {"custom": {"apiKey": "${MINI_NANOBOT_API_KEY}", "apiBase": "${MINI_NANOBOT_API_BASE}"}},
              "contextBudget": 24000, "tools": {"exec": {"timeout": 120, "sandbox": ""}}}
    with path.open("x") as stream:
        stream.write(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)
    typer.echo(f"已创建配置：{path}，请填写项目的 .env。")


@app.command()
def web(
    ctx: typer.Context,
    port: int = typer.Option(8765, "--port", "-p", min=1, max=65535, help="本地服务端口"),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w", exists=True, file_okay=False, help="默认代码目录"),
    config: Path | None = typer.Option(None, "--config", "-c", exists=True, dir_okay=False),
    dev: bool = typer.Option(False, "--dev", help="同时启动前端热更新"),
    no_open: bool = typer.Option(False, "--no-open", help="不自动打开浏览器"),
):
    """启动本地前后端；在 VS Code 中可按 F5 运行。"""
    from aiohttp import web as aiohttp_web

    from mini_nanobot.server import access_token, create_app

    settings = load_settings(ctx.obj, config)
    settings.workspace = workspace.resolve()
    token = access_token(settings.home)
    frontend_port = 5173 if dev else port
    url = f"http://127.0.0.1:{frontend_port}/#token={token}"
    process = None
    if dev:
        import os
        source = Path(__file__).parent.parent / "webui"
        if not (source / "node_modules" / "vite").is_dir():
            raise typer.BadParameter("请先在 webui 目录执行 npm install。")
        process = subprocess.Popen(["npm", "run", "dev", "--", "--host", "127.0.0.1"], cwd=source,
                                   env={**os.environ, "MINI_API_URL": f"http://127.0.0.1:{port}"})
    typer.echo("mini_nanobot 已准备启动，保持终端开启；Ctrl+C 停止。")
    typer.echo("本机登录链接（包含访问码，请勿分享）：\n" + url)
    application = create_app(settings, token=token)

    async def open_browser(application):
        if not no_open:
            await asyncio.to_thread(webbrowser.open, url)
    application.on_startup.append(open_browser)
    try:
        aiohttp_web.run_app(application, host="127.0.0.1", port=port, print=None)
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


#在这里会启动调用agent
@app.command()
def code(
    ctx: typer.Context,
    workspace: Path = typer.Option(Path("."), "--workspace", "-w", exists=True, file_okay=False, help="代码目录"),
    message: str | None = typer.Option(None, "--message", "-m", help="单次任务，省略则交互对话"),
    session: str | None = typer.Option(None, "--session", "-s", help="续接精简版会话 ID"),
    config: Path | None = typer.Option(None, "--config", "-c", exists=True, dir_okay=False),
):
    """在终端运行与网页相同的编程助手。"""
    settings = load_settings(ctx.obj, config)

    async def run():
        service = AgentService(settings)
        try:
            item = service.store.get(session) if session else service.create_session(str(workspace))
            typer.echo(f"会话 ID：{item['id']}\n代码目录：{item['workspace']}")

            async def output(event):
                if event["type"] == "text":
                    typer.echo(event["text"], nl=False)
                elif event["type"] == "task":
                    typer.echo(f"\n[子任务 {event['role']}：{event['state']}]")
                elif event["type"] == "error":
                    typer.echo("\n" + event["message"], err=True)
            current = message
            while True:
                if current is None:
                    current = await asyncio.to_thread(input, "\n你：")
                if current.strip() in {"exit", "quit", "退出"}:
                    break
                await service.start(item["id"], current, output)
                typer.echo()
                if message is not None:
                    if service.store.get(item["id"])["status"] != "idle":
                        raise typer.Exit(1)
                    break
                current = None
        finally:
            await service.close()
    asyncio.run(run())


@app.command()
def version():
    """显示当前精简版版本。"""
    typer.echo(f"mini_nanobot {__version__}")


def main():
    try:
        app()
    except (ValueError, OSError) as error:
        typer.echo(f"启动失败：{error}", err=True)
        raise SystemExit(1) from None
