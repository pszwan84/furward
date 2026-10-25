"""Pure in-memory SDK contract peer. No ROS imports, sockets or physical I/O.

Exercises the production T1HardwareAdapter lifecycle, not the SDK's generated
serialization, DDS, firmware, kinematics, or actual stopping behavior.
"""
import time
import uuid

CONFIG = dict(reviewed=True, source='t1_simulation', expected_source='', priority=30,
              domain_id=213, source_timeout_ms=500, rpc_timeout=.3,
              forward_speed=.21, angular_speed=.3)


class SimulatedSdkTransport:
    simulated = True

    def __init__(self, config):
        self.source = config['source'] + '_' + uuid.uuid4().hex[:12]
        self.on_asr = self.on_fault = None
        self.owner, self.registered = config['expected_source'], False
        self.action, self.closed = 102, False
        self.battery = None
        self.calls = []

    def connect(self):
        self.calls.append(('connect',))

    def query_mode(self):
        return dict(action=self.action, status=100, switch_ready=True)

    def query_source(self):
        return self.owner

    def register(self):
        self.calls.append(('register', self.source))
        self.registered = True
        return {'accepted': True}

    def unregister(self):
        self.calls.append(('unregister', self.source))
        self.registered = False
        if self.owner == self.source:
            self.owner = ''
        return {'accepted': True}

    def publish(self, forward, angular):
        self.calls.append(('velocity', float(forward), float(angular), time.monotonic()))
        if self.registered:
            self.owner = self.source

    def set_action(self, action):
        self.calls.append(('action', action))
        self.action = action
        return dict(accepted=True, task_id=1, code=0, state=1)

    def mimic(self, name):
        self.calls.append(('mimic', name))
        return dict(accepted=True, task_id=2, code=0, state=400)

    def speak(self, text, interrupt=False):
        self.calls.append(('speak', text, interrupt))
        return dict(accepted=True, heard_verified=False)

    def led(self, mode):
        if mode == 'BLUE_FLOW':
            raise ValueError('unsupported_sdk_effect: BLUE_FLOW')
        self.calls.append(('led', mode))
        return dict(accepted=True, light_verified=False)

    def close(self):
        self.calls.append(('close',))
        self.closed = True
