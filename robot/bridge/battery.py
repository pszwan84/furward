"""Decode the T1 BMS message into a small, honest dict.

Pure function: no ROS import, no I/O. Layout follows aimdk_msgs/msg/Bms
(status bit 0 = charging, bit 2 = discharging, bit 20 = full). Values were
cross-checked against one live read on the real T1 (status=4, current<0,
remaining_capacity_percentage=37), so charging=False / discharging=True.
"""
import math

CHARGING, DISCHARGING, FULL = 0, 2, 20
FAULT_BITS = {1: 'charge_overcurrent', 3: 'discharge_overcurrent', 4: 'short_circuit', 5: 'cell_overvoltage',
              6: 'cell_undervoltage', 7: 'pack_overvoltage', 8: 'pack_undervoltage', 11: 'discharge_overtemp',
              12: 'charge_overtemp', 13: 'discharge_undertemp', 14: 'charge_undertemp', 16: 'mosfet_charge_blocked',
              17: 'mosfet_discharge_blocked', 18: 'mosfet_overtemp', 23: 'pack_abnormal'}
LOW_PERCENT = 30
CRITICAL_PERCENT = 20


def _bit(value, index):
    return bool(value >> index & 1)


def decode_bms(message):
    """Return a dict; unknown or malformed values stay None instead of guessed."""
    def get(name):
        return getattr(message, name, None) if not isinstance(message, dict) else message.get(name)

    status = get('status')
    percent = get('remaining_capacity_percentage')
    if type(percent) not in (int, float) or isinstance(percent, bool) or not math.isfinite(percent) \
            or not 0 <= percent <= 100:
        percent = None
    if type(status) is not int or status < 0:
        return {'percent': percent, 'charging': None, 'discharging': None, 'full': None,
                'faults': [], 'level': 'unknown'}
    faults = [name for bit, name in FAULT_BITS.items() if _bit(status, bit)]
    charging, discharging = _bit(status, CHARGING), _bit(status, DISCHARGING)
    if percent is None:
        level = 'unknown'
    elif percent < CRITICAL_PERCENT and not charging:
        level = 'critical'
    elif percent < LOW_PERCENT and not charging:
        level = 'low'
    else:
        level = 'ok'
    return {'percent': percent, 'charging': charging, 'discharging': discharging,
            'full': _bit(status, FULL), 'faults': faults, 'level': level}


def motion_allowed_by_battery(decoded, minimum_percent=60):
    """Conservative gate for the PRD G2/G3 trials: unknown, faulted or low means no."""
    return (decoded.get('percent') is not None and decoded['percent'] >= minimum_percent
            and not decoded.get('faults') and decoded.get('level') == 'ok')
