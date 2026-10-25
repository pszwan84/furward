import asyncio
import math
import os
import time
from .gateway import CAPABILITIES, failure, validate_command


class MockRobot:
    backend = 'mock'

    def __init__(self, time_scale=.03, fail_commands=None):
        if isinstance(time_scale, bool) or not isinstance(time_scale, (float, int)) or not math.isfinite(time_scale) or not 0 <= time_scale <= 10:
            raise ValueError('invalid time_scale')
        self.time_scale = time_scale
        self.fail_commands = set(fail_commands if fail_commands is not None else filter(None, os.environ.get('MOCK_FAIL', '').split(',')))
        if self.fail_commands - set(CAPABILITIES):
            raise ValueError('MOCK_FAIL contains unsupported command')
        self.connected = False
        self._handler = None
        self._generation = 0
        self._interrupt = asyncio.Event()
        self._lock = asyncio.Lock()
        self._state = {'simulated': True, 'hardware_verified': False, 'estimated': True,
                       'source': 'command_integral', 'x': 0., 'y': 0., 'heading_deg': 0.,
                       'action': 'stand', 'busy': False, 'battery': None, 'time_scale': time_scale,
                       'time_basis': 'simulated_time', 'capabilities': list(CAPABILITIES)}

    def set_event_handler(self, handler): self._handler = handler
    async def connect(self): self.connected = True
    async def close(self):
        await self.stop('close')
        self.connected = False
    async def get_state(self):
        return {**self._state, 'connected': self.connected, 'backend': self.backend}

    def _integrate(self, name, args, fraction=1.):
        if name == 'move':
            heading = math.radians(self._state['heading_deg'])
            self._state['x'] += args['distance_m'] * fraction * math.cos(heading)
            self._state['y'] += args['distance_m'] * fraction * math.sin(heading)
        if name == 'turn':
            self._state['heading_deg'] += args['angle_deg'] * fraction

    async def stop(self, command_id):
        self._generation += 1
        self._interrupt.set()
        self._interrupt = asyncio.Event()
        self._state['busy'] = False
        return {'ok': True, 'detail': {'simulated': True, 'hardware_verified': False,
                                      'stop_acknowledged': True, 'physical_stationary_confirmed': False}}

    async def execute(self, name, args, command_id, deadline_ms=12000):
        try: args = validate_command(name, args)
        except ValueError as exc: return failure('invalid_command', str(exc))
        if name == 'stop': return await self.stop(command_id)
        if not self.connected: return failure('disconnected')
        if type(deadline_ms) is not int or not 1 <= deadline_ms <= 60000: return failure('invalid_deadline')
        if self._lock.locked(): return failure('busy')
        generation, interrupt = self._generation, self._interrupt
        async with self._lock:
            if generation != self._generation: return failure('interrupted')
            if name in self.fail_commands: return failure('injected_failure')
            duration = {'stand_up': 3, 'mimic': 4, 'posture': 2}.get(name, 0)
            if name == 'move': duration = abs(args['distance_m']) / .3
            if name == 'turn': duration = abs(args['angle_deg']) / 45
            self._state['busy'] = True
            started = time.monotonic()
            try:
                await asyncio.wait_for(interrupt.wait(), timeout=min(duration * self.time_scale, deadline_ms / 1000))
                fraction = min(1., (time.monotonic() - started) / max(duration * self.time_scale, .000001))
                self._integrate(name, args, fraction)
                return {'ok': False, 'error': 'CANCELLED', 'detail': {'progress_fraction': fraction,
                        'simulated': True, 'hardware_verified': False}}
            except asyncio.TimeoutError:
                if duration * self.time_scale > deadline_ms / 1000: return failure('timeout')
                if generation != self._generation: return failure('interrupted')
                self._integrate(name, args)
                if name in ('posture', 'stand_up'): self._state['action'] = args.get('name', 'stand')
                return {'ok': True, 'detail': {'simulated': True, 'hardware_verified': False, 'time_basis': 'simulated_time'}}
            finally: self._state['busy'] = False
