"""Read-only routes that let the ver2 phone/showcase pages talk to this backend.

Nothing here dispatches a robot command. Every route reads static files or an
Engine snapshot, so a GET can never move the robot.
"""
import json
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

DATA_DIR = Path(__file__).parent / 'data'
STATIC_DIR = Path(__file__).parent / 'static'
VERSION = '0.2.0-ver2-compat'


def _wrap(data):
    return {'ok': True, 'data': data}


def _failure(code, message, status):
    return JSONResponse({'ok': False, 'error': code, 'message': message}, status_code=status)


def register_public_routes(app: FastAPI) -> None:
    router = APIRouter()

    def engine_of(request: Request):
        # The default Engine is created inside the app lifespan, so look it up per request.
        return request.app.state.engine

    @router.get('/api/config/public')
    async def public_config(request: Request):
        engine = engine_of(request)
        # No third-party keys are ever served; LLM and map keys stay disabled here.
        health = await engine.health()
        indoor = await engine.indoor_map()
        mode = await engine.mode()
        nodes = indoor.get('nodes', []) if isinstance(indoor, dict) else []
        listing = [{'id': node['id'], 'name': node.get('name', node['id'])} for node in nodes
                   if isinstance(node, dict) and 'id' in node]
        return _wrap({
            'version': VERSION,
            'mode': mode.get('mode') if isinstance(mode, dict) else mode,
            'amap_js_key': '', 'amap_security_code': '',
            'has_llm': False, 'obstacle_source': 'none',
            'indoor_waypoints': listing,
            'simulated': bool(health.get('simulated', True)),
            'hardware_verified': False,
        })

    @router.get('/api/demo/city')
    async def demo_city():
        path = DATA_DIR / 'demo_city.json'
        try:
            return _wrap(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            return _failure('not_found', '演示城市地图不存在', 404)

    @router.get('/api/tasks/current')
    async def tasks_current(request: Request):
        engine = engine_of(request)
        current = await engine.current()
        return _wrap(current.get('task') if isinstance(current, dict) else None)

    @router.get('/api/vision/snapshot.jpg')
    async def snapshot():
        # Real camera frames are a later, separately verified feature (PRD P1).
        return _failure('not_supported', '相机画面尚未接入；视觉能力不参与运动放行', 501)

    @router.get('/showcase', include_in_schema=False)
    async def showcase():
        page = STATIC_DIR / 'showcase.html'
        if not page.is_file():
            return _failure('not_found', '展示页不存在', 404)
        return FileResponse(page, media_type='text/html')

    @router.get('/assist', include_in_schema=False)
    async def assist():
        # ver2 phone page for the participant; talks to the same read-only/limited API.
        page = STATIC_DIR / 'assist.html'
        if not page.is_file():
            return _failure('not_found', '手机页不存在', 404)
        return FileResponse(page, media_type='text/html')

    @router.get('/city3d', include_in_schema=False)
    async def city3d():
        page = STATIC_DIR / 'city3d.html'
        if not page.is_file():
            return _failure('not_found', '3D 页面不存在', 404)
        return FileResponse(page, media_type='text/html')

    app.include_router(router)
