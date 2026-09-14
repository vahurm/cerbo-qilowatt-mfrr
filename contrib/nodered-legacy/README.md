# Legacy Node-RED flow — do not run alongside the agent

`flow.json` here is the **original** Node-RED implementation of the mFRR loop,
from before the Python state machine existed. It is kept for reference and for
sites that were built on it; it is *not* part of the supported install path.

## Why it moved out of the main tree

- It is a second orchestrator. Its actuator nodes write the same dbus paths
  (`/Settings/DynamicEss/Mode`, `/Settings/CGwacs/AcPowerSetPoint`) that
  `agent/qw_agent.py` owns. Two writers race, and the loser looks in the log
  exactly like an ordinary end of dispatch.
- It is coarser than the agent: a plain IDLE/ACTIVE toggle with no Mode gate, no
  power gate (a 0 W stand-down is held as a 0 W event), no Q trades, no SOC
  floor handling, no caps, no read-back, no failsafes beyond a timer.
- Every new user who imported it "just to see" ended up with two drivers.

## If you still want Node-RED

You do not need this flow. Set `QW_LOCAL_BRIDGE=1` and let the agent publish
to the local broker; a flow then only **reads**:

| topic              | payload                  |
|--------------------|--------------------------|
| `qw/online`        | `true` / `false` (LWT)   |
| `qw/mfrr_active`   | `on` / `off`             |
| `qw/mfrr_kind`     | `frr` / `trade` / `none` |
| `qw/mfrr_signed_w` | signed W (neg = export)  |
| `qw/mfrr_degraded` | `true` / `false`         |
| `qw/qw_source`, `qw/qw_mode`, `qw/qw_powerlimit`, `qw/qw_connected` | decoded WORKMODE |

That is how site B's PV-curtailment flow works — see
[`../../nodered/curtailment-mfrr-aware.md`](../../nodered/curtailment-mfrr-aware.md).
Those topic names and payloads are pinned by `tests/test_local_bridge_contract.py`.

## If you must import it anyway

1. The tab imports **disabled** — leave it that way while the agent runs.
2. Never enable the "DESS off/on" and "setpoint" exec nodes on a site where
   `/service/qw-agent` is up.
3. Expect no support; open an issue only with the agent path.
