"""Bounded indoor graph planning; never a localization or obstacle-avoidance claim."""

import heapq
import json
import math
import uuid
from pathlib import Path

from .errors import AppError


MAP = json.loads((Path(__file__).parent / "data/indoor_map.json").read_text())


def indoor(origin, destination, floor=None):
    floor = floor or MAP
    nodes = {n["id"]: n for n in floor["nodes"]}
    if origin not in nodes or destination not in nodes or origin == destination:
        raise AppError("INVALID_WAYPOINT", "请选择两个不同的已知航点")
    graph = {key: [] for key in nodes}
    for a, b in floor["edges"]:
        if a not in nodes or b not in nodes:
            raise AppError("INVALID_MAP", "地图连线引用了未知航点")
        distance = math.hypot(nodes[a]["x"]-nodes[b]["x"], nodes[a]["y"]-nodes[b]["y"])
        if not math.isfinite(distance) or not 0 < distance <= 3:
            raise AppError("INVALID_MAP", "地图单段距离必须在0到3米内")
        graph[a].append((b, distance))
        graph[b].append((a, distance))
    queue, visited, path, distance = [(0, origin, [origin])], set(), None, None
    while queue:
        cost, node, route = heapq.heappop(queue)
        if node in visited:
            continue
        visited.add(node)
        if node == destination:
            path, distance = route, cost
            break
        for neighbor, length in graph[node]:
            if neighbor not in visited:
                heapq.heappush(queue, (cost+length, neighbor, route+[neighbor]))
    if path is None:
        raise AppError("NO_ROUTE", "航点之间没有可用路线")
    commands, steps, heading = [], [], 0.0
    for index, (a, b) in enumerate(zip(path, path[1:])):
        dx, dy = nodes[b]["x"]-nodes[a]["x"], nodes[b]["y"]-nodes[a]["y"]
        angle = math.degrees(math.atan2(dy, dx))
        turn = (angle-heading+180) % 360 - 180
        if abs(turn) > 1e-6:
            commands.append({"name": "turn", "args": {"angle_deg": turn}, "waypoint": b})
        length = math.hypot(dx, dy)
        commands.append({"name": "move", "args": {"distance_m": length}, "waypoint": b})
        steps.append({"index": index, "instruction": f"沿模拟连线前往{nodes[b]['name']}，{length:g}米",
                      "distance_m": length, "action": "straight", "end": [nodes[b]["x"], nodes[b]["y"]]})
        heading = angle
    return {"route_id": uuid.uuid4().hex, "mode": "indoor", "coord": "local_m", "synthetic": floor.get("synthetic", True),
            "distance_m": distance, "duration_s": None, "polyline": [[nodes[p]["x"], nodes[p]["y"]] for p in path],
            "steps": steps, "robot_plan": commands, "current_step": 0, "from": origin, "to": destination,
            "estimated": True, "position_source": "command_integral", "hardware_verified": False}


def outdoor(origin, destination):
    if (not isinstance(origin, list) or len(origin) != 2 or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in origin) or
            not -180 <= origin[0] <= 180 or not -90 <= origin[1] <= 90):
        raise AppError("INVALID_COORDINATES", "起点必须是合法经纬度数组")
    if destination not in ("服务台", "演示服务台", "demo_service"):
        raise AppError("OFFLINE_ROUTE_UNAVAILABLE", "离线仅提供演示服务台示例；尚未接入高德实时规划")
    # A relative illustrative line, not a surveyed walkable route or a cloud response.
    end = [round(origin[0]+.0002, 7), origin[1]]
    return {"route_id": uuid.uuid4().hex, "mode": "outdoor", "coord": "gcj02", "cached": True,
            "synthetic": True, "provider": "offline_illustration", "distance_m": None, "duration_s": None,
            "polyline": [origin, end], "steps": [{"index": 0, "instruction": "这是离线路线示意，请由现场人员确认实际通行路线",
                                                   "distance_m": None, "action": "illustration", "end": end}],
            "robot_plan": None, "current_step": 0, "hardware_verified": False}
