"""Explicit single-session bridge. Default mock; native imports only after CLI opt-in."""
import argparse
import asyncio
import ipaddress
import json
import math
import signal
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import websockets

from backend.robot.gateway import CAPABILITIES, SDK_COMMIT, failure
from backend.robot.mock import MockRobot


def validate_url(url, allow_lan=False):
    parsed = urlparse(url)
    if (parsed.scheme != 'ws' or parsed.username or parsed.password or parsed.query or parsed.fragment or
            parsed.path != '/ws/bridge' or not parsed.hostname):
        raise ValueError('requires ws://HOST:PORT/ws/bridge without credentials/query/fragment')
    if parsed.port is None or not 1 <= parsed.port <= 65535:
        raise ValueError('explicit valid backend port required')
    if parsed.hostname in {'127.0.0.1', 'localhost', '::1'}:
        return
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        raise ValueError('LAN endpoint must be an explicit private IP, not a discovery name') from None
    private = (address.version == 4 and any(address in ipaddress.ip_network(block)
               for block in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
    if not allow_lan or not private:
        raise ValueError('non-loopback requires --allow-lan and an explicit private IPv4 endpoint')


async def run(url, time_scale=.03, *, robot=None, allow_lan=False):
    validate_url(url, allow_lan)
    robot = robot if robot is not None else MockRobot(time_scale=time_scale)
    tasks, seen = set(), set()
    events = asyncio.Queue(maxsize=32)
    closed = False

    async def event_handler(event):
        if closed:
            return
        if events.full():
            raise RuntimeError('bridge event queue overflow')
        events.put_nowait(event)

    robot.set_event_handler(event_handler)
    try:
        await robot.connect()
        async with websockets.connect(url, open_timeout=3, close_timeout=1, max_size=16384, max_queue=32) as ws:
            async def send(value):
                await asyncio.wait_for(ws.send(json.dumps(value, allow_nan=False)), 1)

            state = await robot.get_state()
            fenced_asr = state.get('asr_protocol') == 'native-control-v1'
            hello = {'type': 'hello', 'robot': 'T1', 'sdk_commit': SDK_COMMIT,
                        'session': str(uuid.uuid4()), 'capabilities': state.get('capabilities', CAPABILITIES),
                        'simulated': state.get('simulated') is True}
            if fenced_asr:
                hello.update(asr_protocol='native-control-v1',control_generation=state['control_generation'])
            await send(hello)
            ack = json.loads(await asyncio.wait_for(ws.recv(), 3))
            if ack.get('type') != 'hello_ack' or type(ack.get('generation')) is not int:
                raise RuntimeError('bridge handshake rejected')
            generation = ack['generation']
            last_pong = time.monotonic()

            async def heartbeat():
                while True:
                    if time.monotonic() - last_pong > 3:
                        await robot.stop('backend_heartbeat_lost')
                        await ws.close(code=1011, reason='backend heartbeat lost')
                        return
                    await send({'type': 'ping'})
                    await send({'type': 'status', 'state': await robot.get_state()})
                    await asyncio.sleep(1)

            async def forward_events():
                while True:
                    event = await events.get()
                    if event.get('type') == 'asr':
                        keys = ('type', 'text', 'event_id', 'is_final', 'confidence')
                        if fenced_asr:
                            state = await robot.get_state()
                            stamp = event.get('stamp')
                            if (type(stamp) not in (int,float) or not math.isfinite(stamp) or
                                    not 0 <= time.time()-stamp <= 2 or
                                    type(event.get('control_generation')) is not int or
                                    event['control_generation'] != state.get('control_generation') or
                                    event.get('asr_protocol') != 'native-control-v1'):
                                continue
                            keys += ('stamp','control_generation','asr_protocol')
                        # Preserve original generation/stamp even if the socket
                        # suspends after dispatch. Backend independently fences
                        # already-sent events when stop revokes control.
                        await send({key: event[key] for key in keys})
                    elif event.get('type') == 'fault':
                        await robot.stop('native_fault')
                        await ws.close(code=1011, reason='native adapter fault')
                        return

            async def command(message):
                try:
                    result = await robot.execute(message['name'], message['args'], message['id'], message['deadline_ms'])
                except Exception:
                    await robot.stop('command_exception')
                    result = failure('adapter_exception_unknown')
                await send({'type': 'result', 'id': message['id'], 'generation': generation, **result})

            def task_done(task):
                tasks.discard(task)
                if not closed and not task.cancelled() and task.exception() is not None:
                    close_task = asyncio.create_task(ws.close(code=1011, reason='bridge task failed'))
                    tasks.add(close_task)
                    close_task.add_done_callback(tasks.discard)

            for coro in (heartbeat(), forward_events()):
                task = asyncio.create_task(coro)
                tasks.add(task)
                task.add_done_callback(task_done)
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get('type') == 'cmd':
                        command_id = message.get('id')
                        if (message.get('generation') != generation or not isinstance(command_id, str) or
                                not 1 <= len(command_id) <= 128 or command_id in seen or len(seen) >= 4096 or len(tasks) >= 18 or
                                set(message) != {'type', 'id', 'name', 'args', 'generation', 'deadline_ms'}):
                            raise ValueError('duplicate/stale/invalid command or bridge capacity exceeded')
                        seen.add(command_id)
                        task = asyncio.create_task(command(message))
                        tasks.add(task)
                        task.add_done_callback(task_done)
                    elif message.get('type') == 'pong':
                        last_pong = time.monotonic()
                    else:
                        raise ValueError('unexpected message')
            finally:
                await robot.stop('bridge_disconnected')
    finally:
        closed = True
        try:
            await robot.close()
        finally:
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _run_cli(url,time_scale,robot,allow_lan):
    """SIGTERM cancels our owned run task so its stop/close finally executes."""
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(run(url,time_scale,robot=robot,allow_lan=allow_lan))
    terminating = False
    def terminate():
        nonlocal terminating
        if terminating: return
        terminating = True
        task.cancel()
    try:
        loop.add_signal_handler(signal.SIGTERM,terminate)
        handler_set = True
    except NotImplementedError:  # Windows event loop has no signal handlers
        handler_set = False
    try:
        await task
    except asyncio.CancelledError:
        if not terminating: raise  # Preserve asyncio.run's SIGINT cancellation.
    finally:
        if handler_set: loop.remove_signal_handler(signal.SIGTERM)


def main():
    parser = argparse.ArgumentParser(description='T1 SDK bridge; mock by default, native requires explicit reviewed opt-in.')
    parser.add_argument('--backend-url', default='ws://127.0.0.1:18990/ws/bridge')
    parser.add_argument('--adapter', choices=['mock', 't1-sim', 't1'], default='mock')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--allow-physical', action='store_true')
    parser.add_argument('--allow-lan', action='store_true')
    parser.add_argument('--time-scale', type=float, default=.03)
    args = parser.parse_args()
    validate_url(args.backend_url, args.allow_lan)
    robot = None
    if args.adapter == 't1':
        if not args.allow_physical or args.config is None:
            parser.error('t1 requires --config and --allow-physical; do not run before onsite review')
        from backend.main import unique_object
        data = args.config.read_bytes()
        if len(data) > 8192:
            parser.error('native configuration exceeds 8KiB')
        config = json.loads(data, object_pairs_hook=unique_object)
        from bridge.hardware import T1HardwareAdapter
        robot = T1HardwareAdapter(config)
    elif args.config is not None or args.allow_physical:
        parser.error('--config/--allow-physical are only valid for adapter t1')
    elif args.adapter == 't1-sim':
        from bridge.hardware import T1HardwareAdapter
        from bridge.simulated_sdk import CONFIG, SimulatedSdkTransport
        robot = T1HardwareAdapter(CONFIG, transport_factory=SimulatedSdkTransport)
    asyncio.run(_run_cli(args.backend_url,args.time_scale,robot,args.allow_lan))


if __name__ == '__main__':
    main()
