"""
本地 HTTP 服务：会话 API、逐行 JSON 事件流、中文前端与访问校验。
用来连接前端与后端之间的交流的，比如说你在前端输入问题之后，前端会发送请求，然后
app.router会注册相对应的函数，然后启动那函数。
chat函数连接agent.start
"""

import asyncio
import hmac
import json
import secrets
from pathlib import Path

from aiohttp import web

from mini_nanobot.agent import AgentService
from mini_nanobot.config import Settings

SERVICE = web.AppKey("service", AgentService)


def access_token(home: Path) -> str:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "mini_web_token"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32))
    path.chmod(0o600)
    return path.read_text().strip()


def create_app(settings: Settings, service: AgentService | None = None, token: str | None = None) -> web.Application:
    token = token or access_token(settings.home)

    @web.middleware
    async def protect(request, handler):
        if request.host.rsplit(":", 1)[0] not in {"127.0.0.1", "localhost"}:
            return web.json_response({"error": "仅允许通过本机地址访问。"}, status=403)
        if origin := request.headers.get("Origin"):
            allowed = {f"http://{request.host}", "http://127.0.0.1:5173", "http://localhost:5173"}
            if origin not in allowed:
                return web.json_response({"error": "拒绝其他网站发起的请求。"}, status=403)
        if request.path.startswith("/api/") and request.path != "/api/login":
            if not hmac.compare_digest(request.cookies.get("mini_access", ""), token):
                return web.json_response({"error": "请使用启动终端给出的登录链接。"}, status=401)
        try:
            return await handler(request)
        except KeyError:
            return web.json_response({"error": "会话不存在。"}, status=404)
        except (ValueError, TypeError, OSError) as error:
            return web.json_response({"error": str(error)}, status=400)

    app = web.Application(middlewares=[protect], client_max_size=100000)
    app[SERVICE] = service or AgentService(settings)

    async def body(request):
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("请求内容应为 JSON 对象。")
        return value

    async def login(request):
        data = await body(request)
        supplied = data.get("token")
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied, token):
            return web.json_response({"error": "访问码不正确。"}, status=401)
        response = web.json_response({"ok": True})
        response.set_cookie("mini_access", token, httponly=True, samesite="Strict", max_age=2592000)
        return response

    async def status(request):
        return web.json_response({"model": settings.model, "configured": bool(settings.api_key),
                                  "workspace": str(settings.workspace), "max_subagents": settings.max_subagents})

    async def sessions(request):
        if request.method == "GET":
            return web.json_response(app[SERVICE].store.list())
        data = await body(request)
        workspace = data.get("workspace", str(settings.workspace))
        if not isinstance(workspace, str):
            raise ValueError("工作目录应为文本路径。")
        created = app[SERVICE].create_session(workspace)
        return web.json_response(app[SERVICE].snapshot(created["id"]), status=201)

    async def session(request):
        return web.json_response(app[SERVICE].snapshot(request.match_info["id"]))

    async def chat(request):
        identity = request.match_info["id"]
        data = await body(request)
        queue: asyncio.Queue[dict] = asyncio.Queue()
        listening = True

        async def emit(event):
            if listening:
                queue.put_nowait(event)

        app[SERVICE].start(identity, data.get("message"), emit)
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-store"})
        try:
            await response.prepare(request)
            while True:
                event = await queue.get()
                await response.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
                if event["type"] == "done":
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            # 页面断开不自动重放或取消任务；刷新后从会话检查点继续观察。
            pass
        finally:
            listening = False
        return response

    async def stop(request):
        identity = request.match_info["id"]
        app[SERVICE].store.get(identity)
        await app[SERVICE].stop(identity)
        return web.json_response({"ok": True})

    async def memory(request):
        item = app[SERVICE].store.get(request.match_info["id"])
        workspace = Path(item["workspace"])
        if request.method == "PUT":
            value = (await body(request)).get("content")
            if not isinstance(value, str):
                raise ValueError("记忆内容应为文本。")
            app[SERVICE].store.write_memory(workspace, value)
        return web.json_response({"content": app[SERVICE].store.read_memory(workspace)})

    async def frontend(request):
        root = Path(__file__).parent / "web"
        relative = request.match_info.get("path", "")
        if relative.startswith("api/"):
            raise web.HTTPNotFound()
        target = (root / relative).resolve()
        if not target.is_relative_to(root.resolve()):
            raise web.HTTPForbidden()
        if relative and not target.is_file() and "." in Path(relative).name:
            raise web.HTTPNotFound()
        if not target.is_file():
            target = root / "index.html"
        if not target.exists():
            return web.Response(text="请先在 webui 目录运行 npm install 和 npm run build。", status=503)
        return web.FileResponse(target, headers={"Cache-Control": "no-cache"})


    #这些代码是在注册 HTTP 路由：规定前端访问哪个地址、使用哪种请求方式时，由哪个 Python 函数处理。
    app.router.add_post("/api/login", login)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_post("/api/sessions", sessions)
    app.router.add_get("/api/sessions/{id}", session)
    app.router.add_post("/api/sessions/{id}/chat", chat)
    app.router.add_post("/api/sessions/{id}/stop", stop)
    app.router.add_get("/api/sessions/{id}/memory", memory)
    app.router.add_put("/api/sessions/{id}/memory", memory)
    app.router.add_get("/{path:.*}", frontend)

    async def cleanup(application):
        await application[SERVICE].close()
    app.on_cleanup.append(cleanup)
    return app
