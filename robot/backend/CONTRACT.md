# ver1 Backend P0 contract — 2026-10-24

User explicitly selected backend P0. Frontend3D, cloudAPI/LLM, physicalnavigation andvisionmodels are out of this increment.
Preserve the prior guide console and external scripts/server.py/start.sh. New source only backend/, bridge/, tests/test_backend*.

## Root-owned runtime

`backend.runtime.Engine(gateway=None, db_path='var/backend/state.sqlite3')`.
Default `backend.robot.mock.MockRobot`. All methods async except subscribe/unsubscribe.
`start()`, `close()`, `health()`, `robot_state()`, `mode(value=None)`,
`robot_command(name,args,request_id=None)`, `stop()`, `prepare()`, `voice(text)`,
`plan(payload)`, `current()`, `next_step(repeat=False)`, `execute()`, `pause()`, `resume()`, `cancel()`,
`obstacle(value=None)`, `indoor_map()`, `events(after=0)`.
Methods return DATA dict, API wraps `{ok:true,data:...}`. Error `backend.errors.AppError(code,message,status=400)`.
No FastAPI import in runtime. One scheduler task; stop bypasses queue and invalidatesgeneration.
Outdoor forbids move/turn/execute. Mode cannot change duringrunning/paused/preparing task.
Pause/cancel conservatively requestsstop, cannotclaimmeasuredstationary. Sim completion only simulation result.
Unknown physical outcome never automatically retries. Resume requires explicit request and fresh generation.
Restart no taskauto-replay. Indoorposition always `estimated:true` + `source:command_integral`, hardware_verifiedfalse.
Offlineoutdoorroute `cached:true, synthetic:true` fixture, notactualAMAPquery.
`subscribe()->asyncio.Queue` max256 events; `unsubscribe(queue)`. Each message `{type,seq,time,data}`.
1Hz robotstate published byruntime task. Other eventtypes per userver1; no automatic modelclaim.
Runtime owns sqlite history/dedup. API query reads do not emit movement.

## Gateway owned by gateway agent

`backend.robot.gateway.RobotGateway` Protocol:
`backend` attribute `mock` or `bridge`;
async `connect()`, `close()`, `get_state()->dict`,
`execute(name:str,args:dict,command_id:str,deadline_ms:int=12000)->dict`,
`stop(command_id:str)->dict` (independentpriority, notqueued),
`set_event_handler(handler)` sync (handler is async callable receiving eventdict).
Command result `{ok:bool,detail:dict,error?:str,message?:str}`; strict whitelist andfinite bounded args atgatewaytoo.
Capabilitiesnames stand_up,posture,mimic,move,turn,stop,speak,led,snapshot. Snapshotunsupported P1.
Mock event/state all `simulated:true,hardware_verified:false`; nofakephysicalmeasurements. Mockdelay configurable forfasttests.
`BridgeRobot.attach(websocket)` used byAPI `/ws/bridge`, owns single session receive+pings/results.
Hello exactSDKcommit + robotT1 + capabilities + simulated + session; ambiguousduplicates/disconnect =>failclosed.
Onfault/disconnect invokehandler `{type:'fault',...}`. Pending resultunknown =>noautoretry.
Actual bridgeCLI defaultMock only; T1 hardware adapter is explicit unsupported skeleton, no productionROS here.

## API owned by API agent

`backend.main.create_app(engine=None)` injectableEngine; module `app` defaultsMock unlessROBOT_BACKEND=bridge.
FastAPI lifespan awaitsstart/close. All routes ver1RESTtable implemented P0, P1routes audio/location/stream explicitly501.
Defaultoutdoor. Health reveals simulation/backend/capabilities. GET `/api/events?after=0` fornecessaryeventhistory.
`GET /debug` minimalfunctionalresponsiveAPIdebugpage, no3D. `/ws/app` subscribe queue, `/ws/bridge` onlyBridgeRobot.
Commandvalidation via strict Pydantic/finite bounds, HTTP error wrapperincluding422.
POST `/api/mode` `{mode:'outdoor'|'indoor'}`, robotcommand `{name,args,request_id?:str}`,
voice `{text}`, navplanver1payload, obstacledebug `{state:'clear'|'caution'|'stop'}` onlymock.
OtherPOSTactions noextra body required. Errorformat `{ok:false,error:code,message:Chinese}`.
No auth/accountflow forlocalMock. Same-originrequests; defaultloopback, LANexposurefutureexplicit. WSvalidateOrigin;
no wildcardCORS. Singleworkeronly (independent processinstance lockrootSQLite).
APIagent owns backend/main.py,backend/static/*,tests/test_backend_api.py,scripts/backend_browser.mjs,backend/API_CHANGELOG.md.
Gatewayagent owns backend/robot/*,bridge/*,tests/test_backend_gateway.py.
Root owns backend/runtime.py,navigation.py,errors.py,__init__.py,data/*,requirements*/README+CONTRACT and tests/test_backend_runtime.py.
# T1 bridge 0.2 additive contract

`Engine(..., allow_physical=False)` is an independent backend gate. Environment
`ROBOT_ALLOW_PHYSICAL=1` permits non-simulated motion dispatch only while the peer
is connected, ready and fault-free; it never sets hardware_verified true. Mock
remains the default. Every sequence step rechecks the gate. Stop is never gated.
Native CLI separately requires reviewed config and --allow-physical. These flags
are future onsite controls, not authorization to run native ROS during development.

Task.simulated follows gateway state. `detail.sequence_barrier=true` changes a
task to `AWAITING_OPERATOR`, retains step and last_result, blocks new work/mode
changes, and never auto-advances. Only cancel/stop clears software scheduling;
there is no claim that native animation was cancelled or physically completed.
The barrier is used for SetMcMotion whose public response is only acceptance.
Motion command deadlines are 30s; stop transport deadline remains 2s. Open-loop
distance and heading remain command estimates, with physical progress unknown
after interruption. Native motion faults stay latched in the adapter.

Native hello declares `asr_protocol="native-control-v1"` and integer
`control_generation`. ASR carries that protocol, generation and original finite
`stamp` (message-generation UTC seconds, NOT capture time). Both forwarding and
dispatch reject stale/future (>2s/skew), stopped or superseded intents. Backend
has its own immediate stop epoch/cutoff and rechecks before task launch, including
handlers already awaiting storage/state. Native control epochs are never treated
as proof of capture, motion completion or physical rest. Legacy simulation peers
retain their mock-only ASR contract; non-simulated peers must use fenced protocol.
