"""Single-session request/result transport. Unknown outcomes are never replayed."""
import asyncio
import math
import time
from .gateway import CAPABILITIES, SDK_COMMIT, failure, validate_command


class BridgeRobot:
    backend = 'bridge'

    def __init__(self, heartbeat_timeout=5):
        self._ws = None
        self._pending = {}
        self._seen_ids = set()
        self._handler = None
        self._generation = 0
        self._hello = {}
        self._state = {}
        self.heartbeat_timeout = heartbeat_timeout
        self._event_tasks = set()
        self._control_epoch = 0
        self._asr_cutoff = float('-inf')
        self._native_generation_floor = 0

    def set_event_handler(self, handler): self._handler = handler
    async def connect(self): pass
    async def close(self):
        self._control_epoch += 1
        self._asr_cutoff = time.time()
        if self._ws is not None: await self._ws.close(code=1001)
        self._fail_pending('disconnected_unknown')
        pending = list(self._event_tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def _event_done(self, task):
        self._event_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None and self._ws is not None:
            asyncio.create_task(self._ws.close(code=1011))

    def _fail_pending(self, error):
        for future in self._pending.values():
            if not future.done(): future.set_result(failure(error))
        self._pending.clear()

    async def get_state(self):
        return {**self._state, 'backend': 'bridge', 'connected': self._ws is not None,
                'simulated': self._hello.get('simulated', False), 'hardware_verified': False,
                'capabilities': self._hello.get('capabilities', [])}

    def asr_current(self, event):
        """Receipt is not capture: native freshness uses original message stamp.

        Independent backend epoch also invalidates handlers already awaiting
        storage/state when stop arrives. It is checked again before launching.
        """
        if (self._ws is None or event.get('generation') != self._generation or
                event.get('session') != self._hello.get('session') or
                event.get('control_epoch') != self._control_epoch):
            return False
        if self._hello.get('asr_protocol') == 'native-control-v1':
            stamp = event.get('stamp')
            return (event.get('asr_protocol') == 'native-control-v1' and
                    type(event.get('control_generation')) is int and
                    event['control_generation'] >= self._native_generation_floor and
                    type(stamp) in (int,float) and math.isfinite(stamp) and
                    0 <= time.time()-stamp <= 2 and stamp > self._asr_cutoff)
        return self._hello.get('simulated') is True

    async def attach(self, websocket):
        if self._ws is not None:
            await websocket.close(code=1008, reason='single bridge session only')
            return
        # Reserve before first await so concurrent handshakes cannot both win.
        self._ws = websocket
        self._generation += 1
        generation = self._generation
        try:
            await websocket.accept()
            hello = await asyncio.wait_for(websocket.receive_json(), 3)
            if (hello.get('type') != 'hello' or hello.get('robot') != 'T1' or hello.get('sdk_commit') != SDK_COMMIT
                    or type(hello.get('simulated')) is not bool or not isinstance(hello.get('session'), str)
                    or not 1 <= len(hello['session']) <= 128 or not isinstance(hello.get('capabilities'), list)
                    or any(not isinstance(c, str) or c not in CAPABILITIES for c in hello['capabilities'])
                    or 'stop' not in hello['capabilities']):
                raise ValueError('invalid bridge hello')
            fenced_asr = hello.get('asr_protocol') == 'native-control-v1'
            if (not hello['simulated'] and not fenced_asr) or (fenced_asr and
                    (type(hello.get('control_generation')) is not int or hello['control_generation'] < 0)):
                raise ValueError('native bridge requires generation/stamp ASR protocol')
            self._hello = hello
            self._control_epoch += 1
            self._native_generation_floor = hello.get('control_generation',0)
            self._seen_ids.clear()
            await websocket.send_json({'type': 'hello_ack', 'generation': generation, 'sdk_commit': SDK_COMMIT})
            while True:
                message = await asyncio.wait_for(websocket.receive_json(), self.heartbeat_timeout)
                kind = message.get('type')
                if kind == 'ping':
                    await websocket.send_json({'type': 'pong'})
                elif kind == 'ack':
                    if message.get('generation') != generation or message.get('id') not in self._pending:
                        raise ValueError('stale acknowledgement')
                elif kind == 'asr':
                    if (not isinstance(message.get('text'), str) or len(message['text']) > 300 or
                            not isinstance(message.get('event_id'), str) or not 1 <= len(message['event_id']) <= 128 or
                            type(message.get('is_final')) is not bool or
                            type(message.get('confidence')) not in (float, int) or
                            not math.isfinite(message['confidence']) or not 0 <= message['confidence'] <= 1):
                        raise ValueError('invalid ASR message')
                    if self._handler:
                        if len(self._event_tasks) >= 16:
                            raise ValueError('ASR event queue full')
                        event = {key: message[key] for key in ('type', 'text', 'event_id', 'is_final', 'confidence')}
                        event.update(received_at=time.time(), session=self._hello['session'], generation=generation,
                                     control_epoch=self._control_epoch)
                        if fenced_asr:
                            event.update({key:message.get(key) for key in ('stamp','control_generation','asr_protocol')})
                        if not self.asr_current(event): continue
                        if fenced_asr:
                            self._native_generation_floor = max(self._native_generation_floor,event['control_generation'])
                        task = asyncio.create_task(self._handler(event))
                        self._event_tasks.add(task)
                        task.add_done_callback(self._event_done)
                elif kind == 'status':
                    state = message.get('state')
                    if not isinstance(state, dict): raise ValueError('invalid status')
                    native_generation = state.get('control_generation')
                    if fenced_asr and type(native_generation) is int and native_generation >= 0:
                        self._native_generation_floor = max(self._native_generation_floor,native_generation)
                    self._state = {**state, 'hardware_verified': False}
                elif kind == 'result':
                    if message.get('generation') != generation: raise ValueError('stale generation')
                    future = self._pending.get(message.get('id'))
                    if future is None: raise ValueError('unknown or duplicate result')
                    if type(message.get('ok')) is not bool or not isinstance(message.get('detail', {}), dict):
                        raise ValueError('invalid result')
                    self._pending.pop(message['id'])
                    result = {key: message[key] for key in ('ok', 'detail', 'error', 'message') if key in message}
                    result['detail'] = {**result.get('detail', {}), 'simulated': self._hello['simulated'],
                                        'hardware_verified': False}
                    native_generation = result['detail'].get('control_generation')
                    if fenced_asr and type(native_generation) is int and native_generation >= 0:
                        self._native_generation_floor = max(self._native_generation_floor,native_generation)
                    future.set_result(result)
                else: raise ValueError('unsupported bridge message')
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._control_epoch += 1
            self._ws = None
            self._hello = {}
            self._state = {}
            self._fail_pending('disconnected_unknown')
            pending = list(self._event_tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            try: await websocket.close(code=1008)
            except Exception: pass
            if self._handler: await self._handler({'type': 'fault', 'error': 'bridge_disconnected', 'hardware_verified': False})

    async def execute(self, name, args, command_id, deadline_ms=12000):
        try: args = validate_command(name, args)
        except ValueError as exc: return failure('invalid_command', str(exc))
        if type(deadline_ms) is not int or not 1 <= deadline_ms <= 60000: return failure('invalid_deadline')
        if name == 'stop':
            # Revoke before any socket await, including callers using execute.
            self._control_epoch += 1
            self._asr_cutoff = time.time()
            if self._hello.get('asr_protocol') == 'native-control-v1':
                self._native_generation_floor += 1
        if self._ws is None or not self._hello: return failure('disconnected')
        if name not in self._hello['capabilities']: return failure('unsupported_capability')
        if not isinstance(command_id, str) or not command_id or len(command_id) > 128 or command_id in self._seen_ids:
            return failure('invalid_command_id')
        self._seen_ids.add(command_id)
        future = asyncio.get_running_loop().create_future()
        self._pending[command_id] = future
        deadline = asyncio.get_running_loop().time() + deadline_ms / 1000
        try:
            await asyncio.wait_for(self._ws.send_json({'type': 'cmd', 'id': command_id, 'name': name, 'args': args,
                                      'generation': self._generation, 'deadline_ms': deadline_ms}), deadline_ms / 1000)
            return await asyncio.wait_for(future, max(0, deadline - asyncio.get_running_loop().time()))
        except asyncio.TimeoutError: return failure('timeout_unknown')
        except Exception: return failure('disconnected_unknown')
        finally: self._pending.pop(command_id, None)

    async def stop(self, command_id):
        # Bypasses other requests; transport acknowledgement is not physical confirmation.
        # Revoke before the first await; old intents cannot become fresh merely
        # because their frame/async handler is delivered after stop.
        return await self.execute('stop', {}, command_id, 2000)
