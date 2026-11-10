"""中文命令入口：初始化配置、启动完整网页、终端对话。"""

import asyncio
import json
import subprocess
import webbrowser
from datetime import datetime, timezone
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


#启动网页
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


@app.command("eval")
def evaluate_tasks(
    ctx: typer.Context,
    cases: list[str] = typer.Option([], "--case", help="任务 ID，可重复传入；省略则全部"),
    modes: list[str] = typer.Option(["single", "serial", "parallel"], "--mode", help="single / serial / parallel / auto / auto_legacy（旧委派提示对照），可重复传入"),
    repeat: int = typer.Option(1, "--repeat", min=1, max=20, help="每任务每模式的独立重复次数"),
    max_calls: int = typer.Option(30, "--max-calls", min=1, max=200, help="每次试验共享的模型调用及 HTTP 请求上限"),
    max_output_tokens: int | None = typer.Option(None, "--max-output-tokens", min=1, max=65536, help="本轮单次模型输出 Token 上限；省略则使用配置"),
    repairs: int = typer.Option(0, "--repairs", min=0, max=2, help="外部验收失败后的业务修复次数，基线默认 0"),
    report_repairs: int = typer.Option(0, "--report-repairs", min=0, max=2, help="交接格式更正次数，基线默认 0"),
    isolated: bool = typer.Option(True, "--isolate/--shared-workspace", help="独立子任务副本；共享模式仅用于基线对照"),
    timeout: int = typer.Option(180, "--timeout", min=1, max=3600, help="每次试验所有角色的总执行秒数"),
    seed: int = typer.Option(42, "--seed", help="试验顺序随机种子；不控制模型随机性"),
    output: Path | None = typer.Option(None, "--output", help="新建结果目录，已有目录不会覆盖"),
    suite: Path | None = typer.Option(None, "--suite", exists=True, dir_okay=False, help="可信任务集 JSON；其中的校验代码会执行"),
    config: Path | None = typer.Option(None, "--config", "-c", exists=True, dir_okay=False),
    list_only: bool = typer.Option(False, "--list", help="只列出任务，不调用模型"),
    check_only: bool = typer.Option(False, "--self-check", help="离线核对原始代码失败、参考代码通过，不调用模型"),
):
    """评测代码修复效果，保存独立代码目录、完整追踪与中文报告。"""
    import uuid

    from mini_nanobot.evaluation import evaluate, load_cases, self_check

    available = load_cases(suite)
    unknown = set(cases) - {case.id for case in available}
    if unknown:
        raise typer.BadParameter("未知任务：" + "、".join(sorted(unknown)))
    selected = [case for case in available if not cases or case.id in cases]
    if list_only:
        for case in selected:
            typer.echo(f"{case.id} [{case.category}] {case.description}")
        return
    target = (output or Path("eval_results") / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6])).resolve()
    if check_only:
        from mini_nanobot.config import Settings
        settings = load_settings(ctx.obj, config) if config else Settings(home=target / "profile")
        report = asyncio.run(self_check(selected, target, settings))
        typer.echo(f"任务集自检：{'通过' if report['passed'] else '失败'}，共 {len(selected)} 项。\n结果：{target / 'self_check.json'}")
        if not report["passed"]:
            raise typer.Exit(1)
        return
    settings = load_settings(ctx.obj, config)
    if not settings.api_key:
        raise typer.BadParameter("真实评测需要模型密钥；请配置 .env，或使用 --self-check 离线验证。")
    if max_output_tokens is not None:
        settings.max_output_tokens = max_output_tokens
    typer.echo(f"即将执行 {len(selected) * len(modes) * repeat} 次独立试验；每次最多 {max_calls} 次调用、{timeout} 秒。")
    report = asyncio.run(evaluate(selected, modes, repeat, settings, target, max_calls, timeout, seed, progress=typer.echo,
                                  max_repairs=repairs, report_repairs=report_repairs, isolated=isolated))
    for row in report["summary"]:
        typer.echo(f"{row['mode']}：成功 {row['success_rate']:.1%}，平均 {row['mean_agent_seconds']:.2f} 秒，平均 {row['mean_model_calls']:.1f} 次调用。")
    typer.echo(f"报告：{target / 'report.md'}\n原始结果：{target / 'report.json'}")


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
