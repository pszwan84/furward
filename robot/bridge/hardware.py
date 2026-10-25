"""Bounded native gateway. Constructors never import ROS or discover devices."""
import asyncio
import math
import re
import threading
import time
from collections import deque
from contextlib import contextmanager

from backend.robot.gateway import CAPABILITIES, SDK_COMMIT, failure, validate_command

SDK_URL = f'https://github.com/PrimebotTech/primebot-t1-sdk/tree/{SDK_COMMIT}'
# Restricted subgraph of official examples/python/common/mc_action_switcher.py
# at SDK_COMMIT. No passive, recovery, transform, or biped destinations.
ACTION_GRAPH = {101: (102, 110, 111), 102: (101, 110, 111),
                110: (101, 111), 111: (101, 110)}
MOTION_COMMANDS = {'stand_up', 'posture', 'mimic', 'move', 'turn'}
NATIVE_ASR_PROTOCOL = 'native-control-v1'


class ControlSuperseded(RuntimeError):
    """An observation belongs to a revoked software control generation."""


class OwnershipLost(RuntimeError):
    """Source mismatch already revoked control and scheduled its fault event."""


def validate_config(config):
    required = {'reviewed', 'source', 'expected_source', 'priority', 'domain_id',
                'source_timeout_ms', 'rpc_timeout', 'forward_speed', 'angular_speed'}
    if not isinstance(config, dict) or set(config) != required or config['reviewed'] is not True:
        raise ValueError('exact reviewed native configuration required')
    if not isinstance(config['source'], str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{2,40}', config['source']):
        raise ValueError('invalid source')
    if not isinstance(config['expected_source'], str) or not re.fullmatch(r'[A-Za-z0-9_./-]{0,80}', config['expected_source']):
        raise ValueError('invalid expected_source')
    for key, low, high in [('priority', 20, 39), ('domain_id', 0, 232), ('source_timeout_ms', 100, 1000)]:
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError('invalid ' + key)
    for key, low, high in [('rpc_timeout', .1, 3), ('forward_speed', .2, .3), ('angular_speed', .2, .5)]:
        value = config[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high or (key != 'rpc_timeout' and value == low):
            raise ValueError('invalid ' + key)
    return dict(config)


class T1HardwareAdapter:
    backend = 't1'
    capabilities = list(CAPABILITIES)

    def __init__(self, config, transport_factory=None):
        self.config = validate_config(config)
        self._factory = transport_factory
        self._transport = None
        self.simulated = False
        self.connected = self._ready = self._closed = False
        self._started = self._registration_attempted = False
        self._generation = 0
        self._lock = asyncio.Lock()
        self._publish_lock = threading.RLock()
        self._target = (0., 0.)
        self._target_until = 0.
        self._fault = None
        self._uncertain = False
        self._active_motion = False
        self._active_rpc = False
        self._velocity_task = self._monitor_task = None
        self._handler = self._loop = None
        self._event_tasks = set()
        self._asr_pending = 0
        self._mode = None
        self._last_action_at = float('-inf')
        self._fault_pending = False
        self._event_lock = threading.Lock()

    def set_event_handler(self, handler): self._handler = handler

    @contextmanager
    def _dispatch(self, generation, deadline=None):
        # Held only for actual ROS call_async dispatch, never service/RPC waits.
        # Stop revokes under this same short lock before publishing zero.
        with self._publish_lock:
            if generation != self._generation or self._closed:
                raise ControlSuperseded('interrupted before dispatch')
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('command deadline expired before dispatch')
            yield

    async def _rpc(self, method, *args, deadline=None, generation=None):
        """Daemon workers leave stop and event loop shutdown independent of RPC.

        A timed out call may still finish. Every later stage is fenced; no retry.
        """
        if generation is not None and (generation != self._generation or self._closed):
            raise ControlSuperseded('interrupted')
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(method + ' deadline expired before dispatch')
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        def deliver(value, error):
            if not future.done():
                if error: future.set_exception(error)
                else: future.set_result(value)
        def worker():
            try:
                if generation is not None and (generation != self._generation or self._closed):
                    raise ControlSuperseded('interrupted')
                if generation is not None and hasattr(self._transport, '_dispatch_context'):
                    self._transport._dispatch_context.guard = lambda:self._dispatch(generation, deadline)
                value, error = getattr(self._transport, method)(*args), None
            except Exception as exc:
                value, error = None, exc
            try: loop.call_soon_threadsafe(deliver, value, error)
            except RuntimeError: pass
        threading.Thread(target=worker, daemon=True, name='t1-' + method).start()
        timeout = self.config['rpc_timeout']
        if deadline is not None: timeout = min(timeout, max(0., deadline - time.monotonic()))
        try:
            value = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(method + ' outcome unknown: timeout; no retry') from exc
        if generation is not None and (generation != self._generation or self._closed):
            raise ControlSuperseded('interrupted')
        return value

    def _zero(self):
        with self._publish_lock:
            self._target = (0., 0.)
            self._target_until = 0.
            if self._transport is not None and self._registration_attempted:
                self._transport.publish(0., 0.)

    def _trip(self, reason, uncertain=True):
        self._fault = str(reason)
        self._ready = False
        self._uncertain |= uncertain
        with self._publish_lock:
            self._generation += 1
            try: self._zero()
            except Exception as exc: self._fault += '; zero failed: ' + str(exc)
        self._queue_event(dict(type='fault',message=self._fault,outcome_unknown=self._uncertain), fault=True)

    async def _velocity(self):
        tick = time.monotonic()
        try:
            while not self._closed:
                with self._publish_lock:
                    target = self._target if time.monotonic() < self._target_until and self._ready else (0., 0.)
                    self._transport.publish(*target)
                tick += .02
                await asyncio.sleep(max(0., tick - time.monotonic()))
                if tick < time.monotonic() - .02: tick = time.monotonic()
        except asyncio.CancelledError: raise
        except Exception as exc: self._trip('velocity publication: ' + str(exc))

    @staticmethod
    def _stable(mode):
        if not isinstance(mode, dict) or mode.get('action') not in ACTION_GRAPH or mode.get('status') != 100 or mode.get('switch_ready') is not True:
            raise RuntimeError('requires stable reviewed quadruped mode')
        return mode['action']

    async def _check(self, deadline=None, generation=None):
        mode = await self._rpc('query_mode', deadline=deadline, generation=generation)
        self._mode = self._stable(mode)
        await self._check_source(deadline,generation)
        return self._mode

    async def _check_source(self, deadline=None, generation=None):
        source = await self._rpc('query_source',deadline=deadline,generation=generation)
        if source != self._transport.source:
            self._trip('input source ownership conflict')
            raise OwnershipLost('input source ownership conflict')

    async def connect(self):
        if self._started or self._closed: raise RuntimeError('connect is one-shot; no reconnect')
        self._started = True
        self._loop = asyncio.get_running_loop()
        if self._factory is None:
            from .t1_transport import T1RosTransport
            self._factory = T1RosTransport
        self._transport = self._factory(self.config)
        self.simulated = getattr(self._transport, 'simulated', False) is True
        self._transport.on_asr = self._asr
        self._transport.on_fault = self._on_fault
        generation = self._generation
        try:
            await self._rpc('connect', generation=generation)
            self._mode = self._stable(await self._rpc('query_mode', generation=generation))
            if self._mode != 102:
                raise RuntimeError('connect requires operator-selected stable mode 102')
            if await self._rpc('query_source', generation=generation) != self.config['expected_source']:
                raise RuntimeError('initial input source conflict')
            self._registration_attempted = True
            await self._rpc('register', generation=generation)
            self._zero()
            self._velocity_task = asyncio.create_task(self._velocity())
            deadline = time.monotonic() + self.config['rpc_timeout']
            while True:
                mode = await self._rpc('query_mode', deadline=deadline, generation=generation)
                self._mode = self._stable(mode)
                source = await self._rpc('query_source', deadline=deadline, generation=generation)
                if source == self._transport.source: break
                if source != self.config['expected_source']: raise RuntimeError('ownership acquisition conflict')
                if time.monotonic() >= deadline: raise TimeoutError('zero ownership acquisition timeout')
                await asyncio.sleep(.01)
            if self._fault: raise RuntimeError(self._fault)
            self.connected = self._ready = True
            self._monitor_task = asyncio.create_task(self._monitor())
        except BaseException:
            await self.close()
            raise

    async def _monitor(self):
        try:
            while True:
                await asyncio.sleep(.1)
                generation = self._generation
                try:
                    # Mode is transient during an action, but ownership must
                    # still be monitored throughout transitions and spacing.
                    if (self._lock.locked() or self._uncertain) and not any(self._target):
                        await self._check_source(generation=generation)
                        continue
                    mode = await self._check(generation=generation)
                    if any(self._target) and mode != 102:
                        raise RuntimeError('locomotion mode changed during movement')
                except ControlSuperseded:
                    # Ordinary stop invalidates in-flight observations. Discard
                    # them and continue; genuine errors/timeouts still trip.
                    continue
        except asyncio.CancelledError: raise
        except OwnershipLost: return
        except Exception as exc: self._trip(exc)

    def _on_fault(self, reason):
        if self._loop and not self._closed:
            self._loop.call_soon_threadsafe(self._trip, str(reason))

    def _asr(self, **event):
        try:
            stamp,confidence = event.get('stamp'),event.get('confidence')
            if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0 <= time.time()-stamp <= 2: return
            if type(confidence) not in (int,float) or not math.isfinite(confidence) or not 0 <= confidence <= 1: return
            if not isinstance(event.get('text'),str) or not 1 <= len(event['text']) <= 300: return
            if type(event.get('is_final')) is not bool or not isinstance(event.get('event_id'),str) or not 1 <= len(event['event_id']) <= 128: return
        except (TypeError,ValueError,OverflowError): return
        self._queue_event({'type':'asr', **event, 'received_at':time.time(),
                           'capture_time_verified':False, 'simulated':self.simulated,
                           'control_generation':self._generation,'asr_protocol':NATIVE_ASR_PROTOCOL})

    def _queue_event(self, event, fault=False):
        if self._closed or not self.connected or not self._handler: return
        generation = self._generation
        with self._event_lock:
            if fault:
                if self._fault_pending: return
                self._fault_pending = True
            else:
                if self._asr_pending >= 16: return
                self._asr_pending += 1
        def enqueue():
            async def emit():
                try:
                    if not self._closed and generation == self._generation and self._handler:
                        await self._handler(event)
                finally:
                    with self._event_lock:
                        if fault: self._fault_pending = False
                        else: self._asr_pending -= 1
            task = asyncio.create_task(emit())
            self._event_tasks.add(task)
            task.add_done_callback(self._event_done)
        try: self._loop.call_soon_threadsafe(enqueue)
        except RuntimeError:
            with self._event_lock:
                if fault: self._fault_pending = False
                else: self._asr_pending -= 1

    def _event_done(self, task):
        self._event_tasks.discard(task)
        if not task.cancelled(): task.exception()

    async def get_state(self):
        return dict(backend=self.backend, simulated=self.simulated, connected=self.connected,
                    ready=self._ready and not self._uncertain, busy=self._lock.locked(), hardware_verified=False,
                    telemetry_verified=False, battery=getattr(self._transport, 'battery', None),
                    control_generation=self._generation,asr_protocol=NATIVE_ASR_PROTOCOL,
                    action=self._mode, fault=self._fault, outcome_unknown=self._uncertain,
                    estimated=True, time_basis='monotonic_open_loop', capabilities=list(self.capabilities))

    async def stop(self, command_id):
        try:
            with self._publish_lock:
                self._generation += 1
                if self._active_rpc and self._active_motion:
                    self._uncertain = True
                self._zero()
        except Exception as exc:
            self._trip(exc)
            return failure('stop_publish_failed', str(exc))
        return {'ok':True, 'detail':dict(stop_acknowledged=True, physical_stationary_confirmed=False,
                                       hardware_verified=False, simulated=self.simulated,
                                       outcome_unknown=self._uncertain,control_generation=self._generation)}

    async def _switch(self, target, deadline, generation):
        current = await self._check(deadline, generation)
        queue = deque([(current, [])])
        seen = {current}
        path = None
        while queue:
            node, route = queue.popleft()
            if node == target: path = route; break
            for dest in ACTION_GRAPH[node]:
                if dest not in seen: seen.add(dest); queue.append((dest, route + [dest]))
        if path is None: raise RuntimeError('no restricted transition path')
        for dest in path:
            while time.monotonic() < self._last_action_at + .5:
                if generation != self._generation: raise RuntimeError('interrupted')
                if time.monotonic() >= deadline: raise TimeoutError('action interval deadline')
                await asyncio.sleep(min(.02,max(0.,deadline-time.monotonic())))
            self._active_rpc = True
            await self._check_source(deadline,generation)
            try: await self._rpc('set_action', dest, deadline=deadline, generation=generation)
            finally: self._last_action_at = time.monotonic()
            while True:
                mode = await self._rpc('query_mode', deadline=deadline, generation=generation)
                await self._check_source(deadline,generation)
                if mode.get('action') not in ACTION_GRAPH: raise RuntimeError('unexpected mode during transition')
                if mode.get('action') == dest and mode.get('status') == 100 and mode.get('switch_ready') is True:
                    self._mode = dest
                    break
                if time.monotonic() >= deadline: raise TimeoutError('action outcome unknown')
                await asyncio.sleep(min(.02, max(0., deadline-time.monotonic())))
            self._active_rpc = False

    async def hold(self, forward, angular, lease=.35):
        """Operator-held motion: fixed reviewed speed, direction only, auto-zero when the lease lapses.

        forward/angular are each -1, 0 or +1 and at most one may be non-zero. The existing 50 Hz
        publisher sends zero as soon as `_target_until` passes, so a crashed or silent client
        cannot leave the robot moving. stop() and any fault revoke it immediately.
        """
        for value in (forward, angular):
            if type(value) is not int or value not in (-1, 0, 1):
                raise ValueError('direction must be -1, 0 or 1')
        if forward and angular:
            raise ValueError('one axis at a time')
        if type(lease) not in (int, float) or not math.isfinite(lease) or not 0 < lease <= .5:
            raise ValueError('lease must be within (0, 0.5] seconds')
        if not self.connected or not self._ready or self._fault:
            return failure('native_unavailable')
        if self._uncertain:
            return failure('outcome_unknown_restart_required')
        if self._lock.locked():
            return failure('busy')
        if not forward and not angular:
            with self._publish_lock:
                self._zero()
            return {'ok': True, 'detail': dict(held=False, hardware_verified=False, simulated=self.simulated)}
        if self._mode != 102:
            return failure('requires_locomotion_mode')
        with self._publish_lock:
            if self._fault or self._closed:
                return failure('native_unavailable')
            self._target = (forward * self.config['forward_speed'], angular * self.config['angular_speed'])
            self._target_until = time.monotonic() + lease
        return {'ok': True, 'detail': dict(held=True, lease_s=lease, hardware_verified=False,
                                           simulated=self.simulated, control_generation=self._generation)}

    async def execute(self, name, args, command_id, deadline_ms=12000):
        try: args = validate_command(name, args)
        except ValueError as exc: return failure('invalid_command', str(exc))
        if name == 'stop': return await self.stop(command_id)
        if name == 'led' and args['mode'] == 'BLUE_FLOW': return failure('unsupported_sdk_effect')
        if type(deadline_ms) is not int or not 1 <= deadline_ms <= 60000: return failure('invalid_deadline')
        if not self.connected or not self._ready: return failure('native_unavailable')
        if self._uncertain and name in MOTION_COMMANDS: return failure('outcome_unknown_restart_required')
        if self._lock.locked(): return failure('busy')
        generation = self._generation
        deadline = time.monotonic() + deadline_ms/1000
        async with self._lock:
            self._active_motion = name in MOTION_COMMANDS
            try:
                detail = await self._command(name,args,deadline,generation)
                if generation != self._generation or self._closed:
                    result = failure('interrupted_unknown')
                else:
                    result = {'ok':True, 'detail':dict(hardware_verified=False, simulated=self.simulated, **detail)}
            except asyncio.CancelledError:
                if self._active_motion: self._uncertain = True
                self._generation += 1
                raise
            except Exception as exc:
                if self._active_motion and (isinstance(exc, TimeoutError) or self._active_rpc): self._uncertain = True
                result = failure('timeout_unknown' if isinstance(exc, TimeoutError) else 'native_command_failed',str(exc))
            finally:
                try: self._zero()
                except Exception as exc:
                    self._trip('command cleanup zero failed: ' + str(exc))
                    result = failure('zero_publish_failed',str(exc))
                self._active_motion = self._active_rpc = False
            return result

    async def _command(self, name, args, deadline, generation):
        if name in ('move','turn'):
            await self._switch(102,deadline,generation)
            value = args['distance_m'] if name == 'move' else math.radians(args['angle_deg'])
            speed = self.config['forward_speed' if name == 'move' else 'angular_speed']
            duration = abs(value)/speed
            if not value: return dict(time_basis='monotonic_open_loop', physical_stationary_confirmed=False)
            # Revalidate after the whole transition path and immediately before
            # making the nonzero target visible to the publisher.
            mode = await self._check(deadline,generation)
            if mode != 102: raise RuntimeError('requires locomotion mode 102')
            until = min(deadline,time.monotonic()+duration)
            with self._publish_lock:
                if generation != self._generation or self._closed: raise RuntimeError('interrupted')
                signed = math.copysign(speed,value)
                self._target = (signed,0.) if name == 'move' else (0.,signed)
                self._target_until = until
            while time.monotonic() < until:
                if generation != self._generation: raise RuntimeError('interrupted')
                await asyncio.sleep(min(.01,until-time.monotonic()))
            if time.monotonic() >= deadline: raise TimeoutError('movement deadline; physical outcome unknown')
            return dict(time_basis='monotonic_open_loop', completion='elapsed', physical_stationary_confirmed=False)
        if name in ('stand_up','posture'):
            target = 101 if name == 'stand_up' else {'stand':101,'sit':111,'lie_down':110}[args['name']]
            await self._switch(target,deadline,generation)
            return dict(mode_observed=target, physical_stationary_confirmed=False)
        if name == 'mimic':
            await self._switch(102,deadline,generation)
            self._active_rpc = True
            result = await self._rpc('mimic',args['motion'],deadline=deadline,generation=generation)
            # Service acceptance does not observe animation completion. Software
            # cancel/stop cannot make a following physical motion safe.
            self._uncertain = True
            return dict(**result, sequence_barrier=True, completion='unobserved')
        self._active_rpc = True
        if name == 'speak': return await self._rpc('speak',args['text'],args.get('interrupt',False),deadline=deadline,generation=generation)
        return await self._rpc('led',args['mode'],deadline=deadline,generation=generation)

    async def close(self):
        if self._closed: return
        self._closed = True
        await self.stop('close')
        self.connected = self._ready = False
        tasks = [task for task in (self._velocity_task,self._monitor_task,*self._event_tasks)
                 if task is not None and task is not asyncio.current_task()]
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        if self._transport is not None:
            if self._registration_attempted:
                try: await self._rpc('unregister')
                except Exception as exc: self._fault = 'release outcome unknown: ' + str(exc)
            try: await self._rpc('close')
            except Exception as exc: self._fault = 'cleanup: ' + str(exc)
