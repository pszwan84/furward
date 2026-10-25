"""One generation-fenced scheduler. All physical metrics remain unverified."""

import asyncio
import copy
import math
import time
import uuid
from contextvars import ContextVar

from guide.store import encoded

from backend.llm_normalizer import normalize_phrase
from .errors import AppError
from .navigation import MAP, indoor, outdoor
from .robot.mock import MockRobot
from .store import BackendStore


BUSY = {"RUNNING", "QUEUED", "PAUSED", "AWAITING_OPERATOR"}
MOVING = {"move", "turn", "stand_up", "posture", "mimic"}


class Engine:
    def __init__(self, gateway=None, db_path="var/backend/state.sqlite3", allow_physical=False, llm_client=None):
        if type(allow_physical) is not bool:
            raise ValueError('allow_physical must be an explicit boolean')
        self.gateway = gateway or MockRobot()
        self.allow_physical = allow_physical
        self.llm_client = llm_client  # optional phrase normalizer; never decides motion
        self._dispatch_simulated = self.gateway.backend == 'mock'
        self.db_path, self.store = db_path, None
        self.generation = 0
        self.worker = None
        self.ticker = None
        self.closed = False
        self.interruptions = 0
        self.subscribers = set()
        self.sequence, self.index, self.nav_worker = [], 0, False
        self.clear_since = time.monotonic() - 1.5
        self._asr_epoch = 0
        self._asr_cutoff = float('-inf')
        self._asr_context = ContextVar('robot_asr_control',default=None)
        self.s = dict(boot_id=uuid.uuid4().hex, revision=0, demo_mode="outdoor", route=None, task=None,
                      nav=dict(status="IDLE", current_step=0, pause_reason=None, estimated=True,
                               estimated_pose={"x":0., "y":0., "heading":0.},
                               position_source="command_integral", remaining_unknown=False),
                      obstacle=dict(state="clear", source="mock", simulated=True, timestamp=time.time()),
                      hardware_verified=False, last_fault=None)

    async def start(self):
        if self.store:
            return
        self.store = BackendStore(self.db_path)
        try:
            prior = self.store.load()
            if prior:
                self.s["demo_mode"] = prior["demo_mode"]
                self.s["route"] = prior.get("route")
                self.s["revision"] = prior["revision"]
                if (prior.get("task") or {}).get("status") in BUSY or prior["nav"]["status"] in {"ACTIVE", "PAUSED"}:
                    self.s["nav"].update(status="FAULT", remaining_unknown=True)
                    self.s["last_fault"] = "上次运行未完成，禁止自动重放；需重新确认起点并规划"
            self.gateway.set_event_handler(self._robot_event)
            await self.gateway.connect()
            self._commit("boot", {"recovered": bool(prior), "auto_replay": False})
            self.ticker = asyncio.create_task(self._tick())
        except BaseException:
            self.store.close()
            self.store = None
            await self.gateway.close()
            raise

    def _busy(self):
        return self.interruptions > 0 or (self.s["task"] is not None and self.s["task"]["status"] in BUSY)

    def _fence_asr(self):
        self._asr_epoch += 1
        self._asr_cutoff = time.time()

    def _asr_current(self, event, epoch):
        if self.closed or epoch != self._asr_epoch: return False
        validator = getattr(self.gateway,'asr_current',None)
        if validator is not None and not validator(event): return False
        if event.get('asr_protocol') == 'native-control-v1':
            stamp = event.get('stamp')
            if (type(stamp) not in (int,float) or not math.isfinite(stamp) or
                    not 0 <= time.time()-stamp <= 2 or stamp <= self._asr_cutoff): return False
        return True

    def _require_asr_current(self):
        control = self._asr_context.get()
        if control is not None and not self._asr_current(*control):
            raise AppError('CONTROL_CHANGED','控制状态已变化，已丢弃迟到语音指令',409)

    def _commit(self, kind, data, command=None):
        self.s["revision"] += 1
        try:
            seq = self.store.save(self.s, kind, data, command)
        except Exception:
            self._fence_asr()
            self.generation += 1
            self.s["nav"].update(status="FAULT", remaining_unknown=True)
            self.s["last_fault"] = "持久化失败；已撤销后续命令"
            asyncio.create_task(self.gateway.stop(uuid.uuid4().hex))
            raise AppError("STORAGE_FAILED", "状态保存失败，已撤销执行", 503) from None
        event = {"type": kind, "seq": seq, "time": time.time(), "data": copy.deepcopy(data)}
        for queue in list(self.subscribers):
            if queue.full():
                # Slow spectators lose history, never block control. Re-read /api/events.
                queue.get_nowait()
            queue.put_nowait(event)

    def subscribe(self):
        queue = asyncio.Queue(maxsize=256)
        self.subscribers.add(queue)
        return queue

    async def _durable_read(self, function, *args):
        try:
            return function(*args)
        except Exception:
            try:
                await self._interrupt("FAULT", "storage_read_failure")
            except Exception:
                self.generation += 1
                self.s["nav"].update(status="FAULT", remaining_unknown=True)
            raise AppError("STORAGE_FAILED", "持久化读取失败，已撤销当前执行", 503) from None

    def unsubscribe(self, queue):
        self.subscribers.discard(queue)

    async def _tick(self):
        while not self.closed:
            try:
                state = await self.robot_state()
                self._commit("robot.state", state)
            except Exception:
                pass
            await asyncio.sleep(1)

    async def health(self):
        state = await self.gateway.get_state()
        return {"version":"ver1-t1-bridge-0.2.0", "backend":self.gateway.backend, "online":state.get("connected", False),
                "simulated":state.get("simulated", self.gateway.backend == "mock"), "hardware_verified":False,
                "boot_id":self.s["boot_id"], "physical_execution_enabled":self._physical_enabled(state)}

    def _physical_enabled(self, state):
        return (self.allow_physical and self.gateway.backend == 'bridge' and
                state.get('simulated') is False and state.get('ready') is True and
                state.get('connected') is True and not state.get('fault'))

    async def robot_state(self):
        state = dict(await self.gateway.get_state())
        state.update(task=copy.deepcopy(self.s["task"]), mode=self.s["demo_mode"], hardware_verified=False,
                     physical_stop_confirmed=False)
        return state

    async def mode(self, value=None):
        if value is not None:
            if value not in {"indoor", "outdoor"}:
                raise AppError("INVALID_MODE", "演示模式必须是indoor或outdoor")
            if self._busy():
                raise AppError("TASK_BUSY", "执行或暂停期间不能切换模式", 409)
            self.s["demo_mode"] = value
            self.s["route"] = None
            self.s["nav"].update(status="IDLE", current_step=0, remaining_unknown=False)
            self._commit("mode.changed", {"mode":value})
        return {"mode": self.s["demo_mode"]}

    async def _validate_motion(self, name):
        self._require_asr_current()
        generation, mode = self.generation, self.s["demo_mode"]
        if self.s["demo_mode"] == "outdoor" and name in {"move", "turn", "nav_execute"}:
            raise AppError("MODE_FORBIDDEN", "室外模式禁止机器狗移动", 409)
        state = await self.gateway.get_state()
        self._require_asr_current()
        if generation != self.generation or mode != self.s["demo_mode"]:
            raise AppError("CONTROL_CHANGED", "控制状态已变化，未执行迟到命令", 409)
        self._dispatch_simulated = state.get('simulated') is True
        if name == "nav_execute" and not self._dispatch_simulated:
            # PRD v0.2: open-loop timed waypoints are never wired to a physical robot.
            raise AppError("TIMED_WAYPOINTS_FORBIDDEN", "定时航点执行仅限模拟；真机只允许操作员持续按住的受控控制", 409)
        if name in MOVING or name == "nav_execute":
            if not self._dispatch_simulated and not self._physical_enabled(state):
                raise AppError("HARDWARE_NOT_RELEASED", "需要显式放行且T1桥接就绪；当前未执行真实运动", 409)

    def _launch(self, commands, nav=False, command=None):
        self._require_asr_current()
        if self._busy():
            raise AppError("TASK_BUSY", "已有单任务在运行或暂停", 409)
        self.generation += 1
        self.sequence, self.index, self.nav_worker = copy.deepcopy(commands), 0, nav
        self.s["task"] = {"task_id":uuid.uuid4().hex, "status":"RUNNING", "step":0,
                          "total_steps":len(commands), "kind":"navigation" if nav else "robot",
                          "simulated":self._dispatch_simulated}
        if command:
            self.s["task"]["request_id"] = command[0]
        if nav:
            self.s["nav"].update(status="ACTIVE", pause_reason=None, remaining_unknown=False)
        self._commit("task.update", self.s["task"], command)
        self.worker = self._create_worker()
        return copy.deepcopy(self.s["task"])

    def _create_worker(self):
        # State is already persisted RUNNING: rejection must now be handled by
        # the worker's terminal-state path, never leave an orphaned busy task.
        initial_asr = self._asr_context.get()
        token = self._asr_context.set(None)
        try: return asyncio.create_task(self._run(self.generation,initial_asr))
        finally: self._asr_context.reset(token)

    async def robot_command(self, name, args, request_id=None):
        if name == "stop":
            return await self.stop()
        if not isinstance(name, str) or type(args) is not dict:
            raise AppError("INVALID_COMMAND", "命令必须包含name与args")
        if request_id is None:
            request_id = uuid.uuid4().hex
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
            raise AppError("INVALID_REQUEST_ID", "request_id长度必须8至128")
        payload = encoded({"name":name,"args":args})
        self._require_asr_current()
        known = await self._durable_read(self.store.known, request_id)
        self._require_asr_current()
        if known is not None:
            if known != payload:
                raise AppError("REQUEST_CONFLICT", "同一request_id不能更换命令", 409)
            return dict(await self._durable_read(self.store.receipt, request_id) or {"status":"UNKNOWN"}, replayed=True)
        await self._validate_motion(name)
        # Gateway performs the same strict command whitelist and parameter checks.
        from .robot.gateway import validate_command
        validate_command(name, args)
        return self._launch([{"name":name,"args":args}], command=(request_id, payload))

    async def prepare(self):
        if self.s["demo_mode"] != "outdoor":
            raise AppError("MODE_FORBIDDEN", "准备序列只用于室外原地展示", 409)
        await self._validate_motion("stand_up")
        route = self.s["route"]
        text = route["steps"][0]["instruction"] if route else "离线准备演示，尚未规划实际路线。"
        return self._launch([{"name":"stand_up","args":{}}, {"name":"led","args":{"mode":"GREEN_FLOW"}},
                             {"name":"mimic","args":{"motion":"GREET"}}, {"name":"speak","args":{"text":text}},
                             {"name":"mimic","args":{"motion":"HEED"}}])

    async def plan(self, payload):
        self._require_asr_current()
        if self._busy():
            raise AppError("TASK_BUSY", "先取消当前任务，再规划新路线", 409)
        if payload.get("mode") != self.s["demo_mode"]:
            raise AppError("MODE_MISMATCH", "路线模式与当前演示模式不同", 409)
        if self.s["demo_mode"] == "indoor":
            route = indoor(payload.get("from"), payload.get("to"))
            start = route["polyline"][0]
            self.s["nav"]["estimated_pose"] = {"x":start[0],"y":start[1],"heading":0.}
        else:
            route = outdoor(payload.get("origin"), payload.get("destination"))
        self.s["route"] = route
        self.s["task"] = None
        self.s["nav"].update(status="PLANNED", current_step=0, remaining_unknown=False, pause_reason=None)
        self._commit("nav.route", route)
        return copy.deepcopy(route)

    async def current(self):
        return {"route":copy.deepcopy(self.s["route"]), "nav":copy.deepcopy(self.s["nav"]),
                "task":copy.deepcopy(self.s["task"]), "hardware_verified":False}

    async def execute(self):
        await self._validate_motion("nav_execute")
        route = self.s["route"]
        if not route or route["mode"] != "indoor" or self.s["nav"]["status"] != "PLANNED":
            raise AppError("NO_ROUTE", "先规划室内航点路线，再执行", 409)
        if self.s["obstacle"]["state"] != "clear":
            raise AppError("OBSTACLE_HOLD", "模拟前方障碍未解除", 409)
        return self._launch(route["robot_plan"], nav=True)

    def _advance_pose(self, command, fraction):
        if not self.nav_worker:
            return
        pose = self.s["nav"]["estimated_pose"]
        if command["name"] == "turn":
            pose["heading"] = (pose["heading"] + command["args"]["angle_deg"]*fraction) % 360
        elif command["name"] == "move":
            distance = command["args"]["distance_m"]*fraction
            pose["x"] += distance*math.cos(math.radians(pose["heading"]))
            pose["y"] += distance*math.sin(math.radians(pose["heading"]))

    async def _run(self, generation, initial_asr=None):
        task_id = self.s["task"]["task_id"]
        try:
            while self.index < len(self.sequence) and generation == self.generation and not self.closed:
                command = self.sequence[self.index]
                await self._validate_motion(command['name'])
                if generation != self.generation or self.closed:
                    return
                # Recheck the original utterance after state validation and
                # immediately before first dispatch. A commit may have crossed
                # its freshness boundary. Later accepted steps use task fencing.
                if initial_asr is not None:
                    if not self._asr_current(*initial_asr):
                        raise AppError('CONTROL_CHANGED','语音指令已过期，未派发动作',409)
                    initial_asr = None
                deadline_ms = 30000 if command['name'] in MOVING else 12000
                result = await self.gateway.execute(command["name"], command["args"], uuid.uuid4().hex, deadline_ms)
                if generation != self.generation or self.closed:
                    if (self.s["task"] and self.s["task"]["task_id"] == task_id and
                            self.s["nav"]["status"] == "PAUSED" and self.nav_worker):
                        fraction = result.get("detail", {}).get("progress_fraction")
                        if result.get("ok") is True:
                            fraction = 1.
                        if (type(fraction) in (int,float) and math.isfinite(fraction) and 0 <= fraction <= 1 and
                                result.get("detail", {}).get("simulated") is True):
                            self._advance_pose(command, fraction)
                            key = "distance_m" if command["name"] == "move" else "angle_deg"
                            if command["name"] in {"move", "turn"}:
                                command["args"][key] *= 1-fraction
                            if fraction >= 1-1e-9:
                                self.index += 1
                        else:
                            self.s["nav"]["remaining_unknown"] = True
                        self._commit("nav.progress", self.s["nav"])
                    return
                if not result.get("ok"):
                    raise AppError(result.get("error","COMMAND_FAILED"), result.get("message","网关执行失败"), 503)
                self.s['task']['last_result'] = copy.deepcopy(result)
                if result.get('detail', {}).get('sequence_barrier') is True:
                    self.s['task']['status'] = 'AWAITING_OPERATOR'
                    self.s['task']['confirmation_reason'] = 'SDK仅受理动作，完成与实际停稳尚未观测；取消任务后现场核对'
                    self._commit('task.update', self.s['task'])
                    return
                self._advance_pose(command, 1)
                self.index += 1
                self.s["task"]["step"] = self.index
                self.s["nav"]["current_step"] = self.index
                self._commit("nav.progress" if self.nav_worker else "task.update",
                             self.s["nav"] if self.nav_worker else self.s["task"])
            if generation == self.generation and not self.closed:
                self.s["task"]["status"] = "DONE"
                if self.nav_worker:
                    self.s["nav"]["status"] = "DONE"
                self._commit("task.update", self.s["task"])
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if generation == self.generation and not self.closed:
                self._fence_asr()
                self.interruptions += 1
                self.generation += 1
                self.s["task"]["status"] = "FAILED"
                self.s["nav"].update(status="FAULT", remaining_unknown=True)
                self.s["last_fault"] = str(exc)
                try:
                    await self.gateway.stop(uuid.uuid4().hex)
                    if self.s["task"] and self.s["task"]["task_id"] == task_id:
                        self._commit("task.update", self.s["task"])
                finally:
                    self.interruptions -= 1

    async def _interrupt(self, status, reason):
        self._fence_asr()
        self.interruptions += 1
        self.generation += 1
        if self.s["task"] and self.s["task"]["status"] in BUSY:
            self.s["task"]["status"] = "PAUSED" if status == "PAUSED" else "CANCELLED"
        self.s["nav"].update(status=status, pause_reason=reason)
        # Stop reaches gateway before storage, logging or speech; no scheduler queue.
        try:
            result = await self.gateway.stop(uuid.uuid4().hex)
            if not result.get("ok"):
                self.s["nav"].update(status="FAULT", remaining_unknown=True)
            if self.worker and not self.worker.done():
                try:
                    await asyncio.wait_for(asyncio.shield(self.worker), .5)
                except asyncio.TimeoutError:
                    self.s["nav"]["remaining_unknown"] = True
            self._commit("nav.progress", self.s["nav"])
            return {"stop_requested":True, "physical_stop_confirmed":False, "result":result,
                    "nav":copy.deepcopy(self.s["nav"])}
        finally:
            self.interruptions -= 1

    async def stop(self):
        return await self._interrupt("CANCELLED", "stop")

    async def pause(self):
        if self.s["nav"]["status"] != "ACTIVE":
            raise AppError("NOT_ACTIVE", "没有运行中的导航可暂停", 409)
        return await self._interrupt("PAUSED", "user")

    async def resume(self):
        if self.interruptions:
            raise AppError("TASK_BUSY", "停车处理尚未返回，禁止启动新命令", 409)
        if self.s["nav"]["status"] != "PAUSED" or not self.nav_worker:
            raise AppError("NOT_PAUSED", "只有当前暂停导航可继续", 409)
        if self.s["nav"]["remaining_unknown"]:
            raise AppError("POSITION_CONFIRMATION_REQUIRED", "剩余路程未知，需取消并重新确认起点规划", 409)
        if self.s["obstacle"]["state"] != "clear" or time.monotonic()-self.clear_since < 1.5:
            raise AppError("OBSTACLE_HOLD", "等待前方清除并稳定1.5秒，再显式继续", 409)
        await self._validate_motion("nav_execute")
        # A sensor/debug update may have arrived while gateway state was awaited.
        if self.s["obstacle"]["state"] != "clear" or time.monotonic()-self.clear_since < 1.5:
            raise AppError("OBSTACLE_HOLD", "状态查询期间前方条件已变化，未恢复执行", 409)
        self.generation += 1
        self.s["task"]["status"] = "RUNNING"
        self.s["nav"].update(status="ACTIVE", pause_reason=None)
        self._commit("nav.progress", self.s["nav"])
        self.worker = self._create_worker()
        return await self.current()

    async def cancel(self):
        return await self._interrupt("CANCELLED", "cancel")

    async def next_step(self, repeat=False):
        route = self.s["route"]
        if not route:
            raise AppError("NO_ROUTE", "尚未规划路线", 409)
        if self._busy():
            raise AppError("TASK_BUSY", "当前有活动任务", 409)
        index = route["current_step"]
        if not repeat:
            index = min(index+1, len(route["steps"])-1)
        route["current_step"] = index
        return await self.robot_command("speak", {"text":route["steps"][index]["instruction"]})

    async def obstacle(self, value=None):
        if value is not None:
            if self.gateway.backend != "mock":
                raise AppError("MOCK_ONLY", "仅Mock模式可以注入障碍", 409)
            if value not in {"clear","caution","stop"}:
                raise AppError("INVALID_OBSTACLE", "障碍状态必须是clear/caution/stop")
            prior = self.s["obstacle"]["state"]
            self.s["obstacle"].update(state=value, timestamp=time.time())
            if value == "clear" and prior != "clear":
                self.clear_since = time.monotonic()
            self._commit("obstacle.update", self.s["obstacle"])
            if value in {"stop", "caution"} and self.s["nav"]["status"] == "ACTIVE":
                # P0 has no validated dynamic velocity update: caution conservatively
                # pauses as well, rather than displaying a warning while continuing.
                await self._interrupt("PAUSED", "obstacle_" + value)
        return copy.deepcopy(self.s["obstacle"])

    async def obstacle_view(self):
        """Public, honest view of the obstacle state (PRD A13).

        There is no real-time vision yet. A simulated peer shows the labeled mock value;
        a physical peer must report 'unknown' because 'no detection' is not 'path is safe'.
        The stored state used by the mock pause logic is left untouched.
        """
        stored = copy.deepcopy(self.s["obstacle"])
        state = await self.gateway.get_state()
        stored["real_time_vision"] = False
        if state.get("simulated") is False:
            stored.update(state="unknown", level="unknown", source="none", simulated=False,
                          note="尚无实时视觉；未检测到目标不等于道路安全")
        else:
            stored.setdefault("level", stored.get("state"))
        return stored

    async def voice(self, text, source="text"):
        self._require_asr_current()
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 300:
            raise AppError("INVALID_TEXT", "请输入1至300字口令")
        original = text
        normalized = await normalize_phrase(text, self.llm_client)
        used_llm = normalized != original
        text = normalized
        value = text.strip().rstrip("。！？!? ")
        if value in {"停","别动","停下","停止"}:
            result = await self.stop()
        elif value in {"停一下","等一下","暂停"}:
            result = await self.pause()
        elif value in {"继续","走吧","继续前进"}:
            result = await self.resume()
        elif value in {"取消","取消导航","取消任务"}:
            result = await self.cancel()
        elif value in {"打个招呼","打招呼"}:
            result = await self.robot_command("mimic", {"motion":"GREET"})
        elif value in {"站起","站起来"}:
            result = await self.robot_command("stand_up", {})
        elif value in {"下一步","重复"}:
            result = await self.next_step(value == "重复")
        elif value.startswith("带我去"):
            labels = {n["name"]:n["id"] for n in MAP["nodes"]}
            target = labels.get(value[3:])
            if target is None:
                raise AppError("UNKNOWN_DESTINATION", "只支持已配置的室内航点")
            await self.plan({"mode":"indoor","from":"start","to":target})
            result = await self.execute()
        else:
            raise AppError("UNSUPPORTED_INTENT", "当前使用有限规则口令，尚未接入LLM自由对话")
        self._commit("voice.heard", {"text":original,"normalized":text if used_llm else None,"source":source,"rule_based":True})
        self._commit("assistant.reply", {"text":"操作已记录；模拟结果不代表真实机身效果","llm":False})
        return {"intent_source":"llm+rules" if used_llm else "rules", "result":result}

    async def _robot_event(self, event):
        if self.closed:
            return
        if event.get("type") == "fault":
            await self._interrupt("FAULT", "bridge disconnected or fault")
            self.s["last_fault"] = str(event.get("message", "bridge fault"))
        elif event.get("type") == "asr":
            epoch = self._asr_epoch
            if not self._asr_current(event,epoch): return
            if (event.get("is_final") is not True or not event.get("event_id") or
                    type(event.get("confidence")) not in (int, float) or
                    not math.isfinite(event["confidence"]) or not .6 <= event["confidence"] <= 1):
                return
            if await self._durable_read(self.store.asr_known, event["event_id"]):
                return
            if not self._asr_current(event,epoch): return
            self.store.save(self.s, "asr.received", {"event_id":event["event_id"]}, asr=event["event_id"])
            token = self._asr_context.set((event,epoch))
            try:
                await self.voice(event.get("text", ""), source="robot_asr")
            except AppError as exc:
                self._commit("assistant.reply", {"text":exc.message,"error":exc.code})
            finally:
                self._asr_context.reset(token)

    async def indoor_map(self):
        return copy.deepcopy(MAP)

    async def events(self, after=0):
        if type(after) is not int or after < 0:
            raise AppError("INVALID_CURSOR", "after必须是非负整数")
        rows = await self._durable_read(self.store.events, after)
        return {"events":rows, "next_after":rows[-1]["id"] if rows else after}

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self._fence_asr()
        self.generation += 1
        if self.ticker:
            self.ticker.cancel()
            await asyncio.gather(self.ticker, return_exceptions=True)
        try:
            await self.gateway.stop(uuid.uuid4().hex)
        finally:
            if self.worker:
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)
            await self.gateway.close()
            if self.store:
                self.store.close()
                self.store = None
