"""Local, single-worker backend HTTP and websocket boundary."""
import json
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

from backend.env_loader import load_env
from backend.errors import AppError
from backend.public_api import register_public_routes
from backend.runtime import Engine


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class ModeBody(StrictBody):
    mode: Literal['outdoor', 'indoor']


class CommandBody(StrictBody):
    name: str = Field(min_length=1, max_length=40)
    args: dict = Field(default_factory=dict)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode='after')
    def validate_args(self):
        from backend.robot.gateway import validate_command
        validate_command(self.name, self.args)
        return self


class VoiceBody(StrictBody):
    text: str = Field(min_length=1, max_length=1000)


class ObstacleBody(StrictBody):
    state: Literal['clear', 'caution', 'stop']


class IndoorPlan(StrictBody):
    mode: Literal['indoor']
    from_: str = Field(alias='from', min_length=1, max_length=80)
    to: str = Field(min_length=1, max_length=80)


class OutdoorPlan(StrictBody):
    mode: Literal['outdoor']
    origin: list[float] = Field(min_length=2, max_length=2)
    destination: str | list[float]


def failure(code, message, status):
    return JSONResponse({'ok': False, 'error': code, 'message': message}, status_code=status)


def same_origin(scope):
    origin = scope.headers.get('origin')
    if not origin:
        return True  # Native local clients have no Origin.
    parsed = urlsplit(origin)
    scheme = 'https' if scope.url.scheme in ('https', 'wss') else 'http'
    return parsed.scheme == scheme and parsed.netloc == scope.headers.get('host') and not parsed.path


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def finite_json(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('nonfinite')
    if isinstance(value, dict):
        for child in value.values():
            finite_json(child)
    elif isinstance(value, list):
        for child in value:
            finite_json(child)


def create_app(engine=None):
    def default_engine():
        load_env()  # local .env; real environment variables take precedence
        backend = os.environ.get('ROBOT_BACKEND', 'mock')
        if backend not in ('mock', 'bridge'):
            raise RuntimeError('ROBOT_BACKEND must be mock or bridge')
        gateway = None
        if backend == 'bridge':
            from backend.robot.bridge import BridgeRobot
            gateway = BridgeRobot()
        allow = os.environ.get('ROBOT_ALLOW_PHYSICAL', '0')
        if allow not in {'0', '1'}:
            raise RuntimeError('ROBOT_ALLOW_PHYSICAL must be 0 or 1')
        from backend.llm_normalizer import client_from_env
        return Engine(gateway=gateway, db_path=os.environ.get('ROBOT_DB_PATH', 'var/backend/state.sqlite3'),
                      allow_physical=allow == '1', llm_client=client_from_env())

    @asynccontextmanager
    async def lifespan(app):
        nonlocal engine
        if engine is None:
            engine = default_engine()
        app.state.engine = engine
        await engine.start()
        try:
            yield
        finally:
            await engine.close()

    app = FastAPI(lifespan=lifespan)
    app.state.engine = engine

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            if not same_origin(request) or request.headers.get('sec-fetch-site') == 'cross-site':
                return failure('origin_forbidden', '仅允许同源请求', 403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    return failure('body_too_large', '请求体超过 16 KiB', 413)
            request._body = bytes(body)
            if body:
                try:
                    finite_json(json.loads(body, object_pairs_hook=unique_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite'))))
                except (ValueError, UnicodeDecodeError, RecursionError):
                    return failure('invalid_json', 'JSON 无效、键重复或包含非有限数', 400)
        return await call_next(request)

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return failure(exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return failure('validation_error', '请求字段或参数不合法', 422)

    from starlette.exceptions import HTTPException

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return failure('http_error', {404: '接口不存在', 405: '请求方法不支持'}.get(exc.status_code, '请求失败'), exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        import logging
        logging.getLogger(__name__).exception('Backend request failed', exc_info=exc)
        return failure('internal_error', '后端内部错误，请检查本地服务日志', 500)

    def wrap(data):
        return {'ok': True, 'data': data}

    def with_online(state):
        # ver2 pages read `online`; keep `connected` untouched for existing clients.
        return {**state, 'online': bool(state.get('connected', False))} if isinstance(state, dict) else state

    for path, method_name in [('/api/health', 'health'), ('/api/robot/state', 'robot_state'),
                              ('/api/mode', 'mode'), ('/api/nav/current', 'current'),
                              ('/api/indoor/map', 'indoor_map'), ('/api/debug/obstacle', 'obstacle_view'),
                              ('/api/vision/obstacle', 'obstacle_view')]:
        def make_get(name):
            async def handler():
                data = await getattr(engine, name)()
                if name == 'robot_state':
                    data = with_online(data)
                return wrap(data)
            return handler
        app.add_api_route(path, make_get(method_name), methods=['GET'])

    @app.post('/api/mode')
    async def mode(body: ModeBody):
        return wrap(await engine.mode(body.mode))

    @app.post('/api/robot/command')
    async def command(body: CommandBody):
        return wrap(await engine.robot_command(body.name, body.args, body.request_id))

    @app.post('/api/voice/text')
    async def voice(body: VoiceBody):
        return wrap(await engine.voice(body.text))

    @app.post('/api/nav/plan')
    async def plan(body: IndoorPlan | OutdoorPlan):
        payload = body.model_dump(by_alias=True)
        if isinstance(body, OutdoorPlan):
            coords = [body.origin] + ([body.destination] if isinstance(body.destination, list) else [])
            if any(len(p) != 2 or abs(p[0]) > 180 or abs(p[1]) > 90 for p in coords):
                return failure('validation_error', '经纬度超出范围', 422)
            if isinstance(body.destination, str) and not 1 <= len(body.destination.strip()) <= 200:
                return failure('validation_error', '目的地无效', 422)
        return wrap(await engine.plan(payload))

    @app.post('/api/debug/obstacle')
    async def obstacle(body: ObstacleBody):
        return wrap(await engine.obstacle(body.state))

    for action in ('stop', 'prepare', 'next', 'repeat', 'execute', 'pause', 'resume', 'cancel'):
        def make_action(name):
            async def handler(request: Request):
                raw = await request.body()
                if raw and json.loads(raw) != {}:
                    return failure('validation_error', '此操作不接受额外字段', 422)
                method = getattr(engine, 'next_step' if name in ('next', 'repeat') else name)
                return wrap(await method(repeat=name == 'repeat')) if name in ('next', 'repeat') else wrap(await method())
            return handler
        prefix = '/api/robot/' if action in ('stop', 'prepare') else '/api/nav/'
        app.add_api_route(prefix + action, make_action(action), methods=['POST'])

    @app.get('/api/events')
    async def events(after: int = 0):
        if after < 0:
            return failure('validation_error', 'after 必须非负', 422)
        return wrap(await engine.events(after=after))

    async def p1():
        return failure('not_implemented', '该能力属于 P1，当前尚未实现', 501)

    for path in ('/api/voice/audio', '/api/nav/location', '/api/location', '/api/vision/stream'):
        app.add_api_route(path, p1, methods=['GET', 'POST'])

    @app.websocket('/ws/app')
    async def app_socket(ws: WebSocket):
        if not same_origin(ws):
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = engine.subscribe()
        import asyncio
        # Per-connection handshake so the ver2 phone/debug pages know the current state.
        await ws.send_json({'type': 'hello', 'seq': 0, 'time': __import__('time').time(),
                            'data': {'mode': (await engine.mode()).get('mode'),
                                     'robot': with_online(await engine.robot_state())}})
        async def receive():
            while True:
                text = await ws.receive_text()
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get('type') == 'ping':
                    await ws.send_json({'type': 'pong', 'seq': 0, 'time': __import__('time').time(), 'data': {}})
        reader = asyncio.create_task(receive())
        pending = None
        try:
            while not reader.done():
                pending = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait((reader, pending), return_when=asyncio.FIRST_COMPLETED)
                if pending in done:
                    message = pending.result()
                    if isinstance(message, dict) and message.get('type') == 'robot.state':
                        message = {**message, 'data': with_online(message.get('data'))}
                    await ws.send_json(message)
                else:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            reader.cancel()
            if pending is not None:
                pending.cancel()
            import anyio
            with anyio.CancelScope(shield=True):
                await asyncio.gather(reader, *([pending] if pending is not None else []), return_exceptions=True)
            engine.unsubscribe(queue)

    @app.websocket('/ws/bridge')
    async def bridge_socket(ws: WebSocket):
        if not same_origin(ws) or getattr(engine.gateway, 'backend', '') != 'bridge':
            await ws.close(code=1008)
            return
        await engine.gateway.attach(ws)

    register_public_routes(app)

    static = Path(__file__).parent / 'static'
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/debug')
    @app.get('/')
    async def debug():
        return FileResponse(static / 'index.html')

    return app


app = create_app()
