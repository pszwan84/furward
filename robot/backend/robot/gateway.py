"""Shared bounded command contract; acknowledgements never certify physical rest."""
import math
from typing import Protocol, Callable, Awaitable

SDK_COMMIT = 'a460300013003b29b9ad291b1b857b0afe295b22'
CAPABILITIES = ['stand_up', 'posture', 'mimic', 'move', 'turn', 'stop', 'speak', 'led']


class RobotGateway(Protocol):
    backend: str
    async def connect(self): ...
    async def close(self): ...
    async def get_state(self) -> dict: ...
    async def execute(self, name: str, args: dict, command_id: str, deadline_ms: int = 12000) -> dict: ...
    async def stop(self, command_id: str) -> dict: ...
    def set_event_handler(self, handler: Callable[[dict], Awaitable[None]]): ...

RobotGatewayProtocol = RobotGateway


def validate_command(name, args):
    fields = {'stand_up': set(), 'stop': set(), 'posture': {'name'}, 'mimic': {'motion'},
              'move': {'distance_m'}, 'turn': {'angle_deg'}, 'speak': {'text', 'interrupt'}, 'led': {'mode'}}
    if name not in fields or not isinstance(args, dict) or set(args) - fields[name]:
        raise ValueError('unsupported command or arguments')
    if name in ('move', 'turn'):
        key, limit = ('distance_m', 3) if name == 'move' else ('angle_deg', 180)
        value = args.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > limit:
            raise ValueError('motion argument must be finite and bounded')
    elif name in ('posture', 'mimic', 'led'):
        key, allowed = {'posture': ('name', {'stand', 'sit', 'lie_down'}),
                        'mimic': ('motion', {'GREET', 'HEED', 'GLANCE', 'HANDSHAKE', 'PONDER'}),
                        'led': ('mode', {'OFF', 'GREEN_FLOW', 'BLUE_FLOW', 'RED_FLASH', 'GREEN', 'BLUE', 'RED'})}[name]
        if not isinstance(args.get(key), str) or args[key] not in allowed:
            raise ValueError('argument is not whitelisted')
    elif name == 'speak':
        if not isinstance(args.get('text'), str) or not 1 <= len(args['text']) <= 1000 or not isinstance(args.get('interrupt', False), bool):
            raise ValueError('invalid speech arguments')
    return dict(args)


def failure(code, message=None):
    return {'ok': False, 'error': code, 'message': message or code, 'detail': {'hardware_verified': False}}
