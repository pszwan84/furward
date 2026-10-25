# T1 bridge 0.2

This page supersedes the historical P0 text below. Native mapping is now
implemented in hardware.py/t1_transport.py against the pinned official SDK.
See ../docs/T1_BRIDGE_DEPLOYMENT.md for exact installation and future onsite steps.

Adapters: mock (default), t1-sim (same T1HardwareAdapter with a pure in-memory
SDK contract peer), t1 (reviewed config + --allow-physical). t1-sim never imports
rclpy or starts DDS. Native execution has NOT been run on hardware; the current
Docker failure prevented actual generated-message/DDS verification of this code.
Synthetic module tests are not generated SDK types.

Native connect is explicit/lazy, requires stable operator-selected mode102 and
the declared input-source handoff. Only quadruped101/102/110/111 state transitions
are permitted; no transform/recovery/passive/damping/joint actions. Independent
50Hz velocity loop, priority stop, generation-revoked dispatch, no reconnect or
unknown-result retry. Constructor performs no ROS import or device access.

Mimic is acceptance only: returns sequence_barrier=true/completion=unobserved;
the backend holds AWAITING_OPERATOR instead of auto-advancing. TTS/LED report
service acceptance only. BLUE_FLOW has no official equivalent and is refused.
Missing/stale BMS is null. ASR stamp means message generation time, not capture;
old/future/nonfinite events and clock skew outside the 2s window are discarded.

Backend additionally requires ROBOT_ALLOW_PHYSICAL=1 for non-simulated movement.
Native CLI requires --config and --allow-physical, with reviewed=true onsite.
Neither flag means measured safe motion or physical verification. Disconnect or
heartbeat loss triggers zero/cleanup; this does not guarantee physical stopping.
Only one bridge/velocity owner must run. Old guide.ros2 is a separate entrypoint.

## Protocol

Mock `--time-scale .03` shortens simulated duration to3%; t1-sim uses actual
monotonic duration with a synthetic SDK peer. Neither is physical timing proof.
`MOCK_FAIL=speak,move` applies only to the mock adapter.

Hello contains robot `T1`, full SDK commit, random session, capability list,
and explicit `simulated`. Backend acknowledges a connection generation.
Each `cmd`/`result` carries matching generation and id. Results may arrive
out of order so stop bypasses a moving command. Every second the bridge sends
status; backend fails closed after5s without incoming status. Duplicate ids,
unknown/stale results, protocol faults, and disconnect never trigger resend.
A timeout has unknown outcome. A stop acknowledgement never confirms physical rest.
Only one session may attach; ambiguous late results close the connection.
At most16 command tasks and32 outbound events are buffered; after4096 command
IDs a new bridge session is required. Status is sent at1Hz and liveness uses
application pongs with a3s bridge watchdog /5s backend receive timeout.
No credential access. Snapshot remains unsupported.

Official source: [fixed SDK](https://github.com/PrimebotTech/primebot-t1-sdk/tree/a460300013003b29b9ad291b1b857b0afe295b22),
vendored protocol notes `../vendor/primebot-t1-sdk/INTERFACES.md` and source hashes `../docs/SDK_SOURCES.json`.
