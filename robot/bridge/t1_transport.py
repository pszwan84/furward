"""ROS2 Humble binding for SDK a460300013003b29b9ad291b1b857b0afe295b22.

Field source: vendored aimdk_msgs generated definitions and INTERFACES.md.
No ROS imports or I/O before connect(). Acquisition belongs to the adapter.
"""
import math
import threading
import time
import uuid
from contextlib import nullcontext

from .hardware import ACTION_GRAPH, validate_config


class T1RosTransport:
    simulated = False

    def __init__(self, config):
        self.config = validate_config(config)
        self.source = config['source'] + '_' + uuid.uuid4().hex[:12]
        self.on_asr = self.on_fault = None
        self._context = self._node = self._executor = self._thread = self._pub = None
        self._closed = self._started = False
        self._resources = threading.RLock()
        self._publish_lock = threading.Lock()
        self._clients = {}
        self._battery_value = self._battery_at = None
        self._dispatch_context = threading.local()

    @property
    def battery(self):
        if self._battery_at is None or time.monotonic() - self._battery_at > 3:
            return None
        return self._battery_value

    def connect(self):
        with self._resources:
            if self._closed or self._started: raise RuntimeError('connect is one-shot; no reconnect')
            self._started = True
            # Separate context/domain; never touch the process-global ROS context.
            import rclpy
            from rclpy.context import Context
            from rclpy.executors import MultiThreadedExecutor
            from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
            from aimdk_msgs.msg import McLocomotionVelocity, AsrResult, Bms
            from aimdk_msgs.srv import (GetMcAction, GetCurrentInputSource, SetMcInputSource,
                                       SetMcAction, SetMcMotion, PlayTts, LedStripCommand)
            try:
                self._context = Context()
                rclpy.init(args=[], context=self._context, domain_id=self.config['domain_id'])
                self._node = rclpy.create_node(self.source, context=self._context)
                self._types = dict(mode=GetMcAction, source=GetCurrentInputSource,
                                   register=SetMcInputSource, action=SetMcAction,
                                   mimic=SetMcMotion, tts=PlayTts, led=LedStripCommand)
                names = dict(mode='GetMcAction', source='GetCurrentInputSource',
                             register='SetMcInputSource', action='SetMcAction',
                             mimic='SetMcMotion', tts='PlayTts', led='LedStripCommand')
                for key, typ in self._types.items():
                    self._clients[key] = self._node.create_client(typ, '/aimdk_5Fmsgs/srv/' + names[key])
                self._velocity_type = McLocomotionVelocity
                self._pub = self._node.create_publisher(McLocomotionVelocity, '/aima/mc/locomotion/velocity', 10)
                qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT,
                                 durability=DurabilityPolicy.VOLATILE)
                self._asr_sub = self._node.create_subscription(AsrResult, '/aima/agent/asr_result', self._asr, qos)
                self._bms_sub = self._node.create_subscription(Bms, '/aima/hal/bms/state', self._bms, qos)
                self._executor = MultiThreadedExecutor(num_threads=3, context=self._context)
                self._executor.add_node(self._node)
                self._thread = threading.Thread(target=self._spin, name='t1-ros-executor', daemon=True)
                self._thread.start()
            except BaseException:
                self.close()
                raise

    def _spin(self):
        try: self._executor.spin()
        except Exception as exc:
            if not self._closed and self.on_fault: self.on_fault('executor: ' + str(exc))

    def _request(self, key):
        if self._closed or self._node is None: raise RuntimeError('native transport unavailable')
        request = self._types[key].Request()
        stamp = self._node.get_clock().now().to_msg()
        if key in ('mode','source','register','led'): request.request.header.stamp = stamp
        elif key == 'tts': request.header.header.stamp = stamp
        else: request.header.stamp = stamp
        return request

    def _call(self, key, request):
        if self._closed: raise RuntimeError('native transport closed')
        deadline = time.monotonic() + self.config['rpc_timeout']
        client = self._clients[key]
        if not client.wait_for_service(timeout_sec=self.config['rpc_timeout']):
            raise TimeoutError(key + ' service unavailable; no retry')
        if self._closed or time.monotonic() >= deadline: raise TimeoutError(key + ' expired before dispatch')
        guard = getattr(self._dispatch_context, 'guard', nullcontext)
        with guard():
            if self._closed: raise RuntimeError('native transport closed before dispatch')
            future = client.call_async(request)
        done = threading.Event()
        future.add_done_callback(lambda _:done.set())
        if not done.wait(max(0., deadline-time.monotonic())):
            future.cancel()
            raise TimeoutError(key + ' outcome unknown: timeout; no retry')
        result = future.result()
        if result is None: raise RuntimeError(key + ' missing response')
        return result

    @staticmethod
    def _accepted(response):
        code, state = response.header.code, response.state.value
        if code != 0 or state not in (1,400):
            raise RuntimeError(f'service refused: code={code} state={state}')
        return dict(accepted=True, code=code, state=state, task_id=response.task_id)

    def query_mode(self):
        result = self._call('mode',self._request('mode'))
        if result.header.code != 0: raise RuntimeError('mode query refused')
        return dict(action=result.info.current_action.value, status=result.info.status.value,
                    switch_ready=result.info.switch_ready)

    def query_source(self):
        result = self._call('source',self._request('source'))
        self._accepted(result.response)
        return result.input_source.name

    def _registration(self, action):
        request = self._request('register')
        request.action.value = action
        request.input_source.name = self.source
        request.input_source.priority = self.config['priority']
        request.input_source.timeout = self.config['source_timeout_ms']
        return self._accepted(self._call('register',request).response)

    def register(self): return self._registration(1001)
    def unregister(self): return self._registration(1003)

    def publish(self, forward, angular):
        # Deliberately no RPC/resource lock: stop can publish during blocked RPC.
        for key,value in [('forward_speed',forward),('angular_speed',angular)]:
            if type(value) not in (int,float) or not math.isfinite(value) or value not in (0.,self.config[key],-self.config[key]):
                raise ValueError('only zero or reviewed velocity allowed')
        with self._publish_lock:
            if self._closed or self._pub is None: raise RuntimeError('native publisher unavailable')
            message = self._velocity_type()
            message.header.stamp = self._node.get_clock().now().to_msg()
            message.source = self.source
            message.forward_velocity = float(forward)
            message.angular_velocity = float(angular)
            message.lateral_velocity = message.pitch_velocity = message.level = 0.
            self._pub.publish(message)

    def set_action(self, action):
        if type(action) is not int or action not in ACTION_GRAPH: raise ValueError('restricted quadruped action required')
        request = self._request('action')
        request.source = self.source
        request.command.action.value = action
        return self._accepted(self._call('action',request).response)

    def mimic(self, name):
        if name not in {'GREET','HEED','GLANCE','HANDSHAKE','PONDER'}: raise ValueError('unsupported mimic')
        request = self._request('mimic')
        request.source = self.source
        request.type = 1  # MIMIC_QY, SetMcMotion.srv
        request.motion = 'QUAD_MIMIC_' + name
        request.model = request.resource = ''
        request.operation = 2  # EXECUTION; never preload or retry.
        request.interrupt = False
        return self._accepted(self._call('mimic',request).response)

    @staticmethod
    def _common(response):
        code, state = response.header.code, response.status.value
        if code != 0 or state != 1: raise RuntimeError(f'service refused: code={code} state={state}')
        return dict(accepted=True, code=code, state=state)

    def speak(self, text, interrupt):
        if not isinstance(text,str) or not 1 <= len(text) <= 1000 or type(interrupt) is not bool:
            raise ValueError('invalid speech arguments')
        request = self._request('tts')
        request.tts_req.text = text
        request.tts_req.domain = self.source
        request.tts_req.trace_id = uuid.uuid4().hex
        request.tts_req.priority_level.value = 6
        request.tts_req.priority_weight = 0
        request.tts_req.is_interrupted = interrupt
        result = self._call('tts',request)
        accepted = self._common(result.header)
        if result.tts_resp.is_success is not True: raise RuntimeError('TTS refused: ' + result.tts_resp.error_message)
        return dict(**accepted, heard_verified=False, trace_id=request.tts_req.trace_id)

    def led(self, mode):
        # SDK has no BLUE_FLOW; do not silently substitute a different effect.
        if mode == 'BLUE_FLOW': raise ValueError('unsupported_sdk_effect: BLUE_FLOW')
        modes = {'OFF':(0,0,0,0,0), 'GREEN_FLOW':(1,0,0,0,0),
                 'RED_FLASH':(11,0,0,0,0),
                 'GREEN':(254,0,255,0,0), 'BLUE':(254,0,0,255,0), 'RED':(254,255,0,0,0)}
        if mode not in modes: raise ValueError('unsupported LED mode')
        request = self._request('led')
        request.led_strip_mode,request.r,request.g,request.b,request.period = modes[mode]
        result = self._common(self._call('led',request).header)
        return dict(**result, light_verified=False, requested_mode=mode)

    def _asr(self, message):
        if self._closed or not self.on_asr: return
        try:
            stamp = message.header.stamp.sec + message.header.stamp.nanosec / 1e9
            confidence = float(message.confidence)
            age = time.time()-stamp
            # Header is message generation time, not capture time. Reject clock
            # skew/future timestamps and >2s old events; receipt alone is unsafe.
            if not math.isfinite(stamp) or not 0 <= age <= 2 or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                return
            if not isinstance(message.text,str) or not 1 <= len(message.text) <= 300: return
            if type(message.is_final) is not bool or not isinstance(message.event_id,str) or not 1 <= len(message.event_id) <= 128: return
            self.on_asr(text=message.text,event_id=message.event_id,is_final=message.is_final,
                        confidence=confidence,stamp=stamp)
        except (AttributeError,TypeError,ValueError,OverflowError): return

    def _bms(self, message):
        value = message.remaining_capacity_percentage
        if type(value) is int and 0 <= value <= 100:
            self._battery_value, self._battery_at = value, time.monotonic()
        else: self._battery_value = self._battery_at = None

    def close(self):
        # No unregister/action/movement here: adapter owns its registration.
        with self._resources:
            if self._closed: return
            with self._publish_lock: self._closed = True
            errors = []
            if self._executor is not None:
                try: self._executor.shutdown(timeout_sec=self.config['rpc_timeout'])
                except Exception as exc: errors.append(str(exc))
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(self.config['rpc_timeout'])
                if self._thread.is_alive(): errors.append('executor thread did not exit within bound')
            for resource in (self._node,self._context):
                if resource is not None:
                    try:
                        if resource is self._node: resource.destroy_node()
                        else: resource.shutdown()
                    except Exception as exc: errors.append(str(exc))
            if errors: raise RuntimeError('; '.join(errors))
