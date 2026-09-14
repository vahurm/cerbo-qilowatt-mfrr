# Safety

This software commands **real grid power flows** and changes inverter behaviour.
Read this before deploying.

## What it controls

- **Dynamic ESS Mode** (`/Settings/DynamicEss/Mode`) — turned off for the duration
  of an mFRR event or a Q trade and restored afterwards.
- **AC power setpoint** (`/Settings/CGwacs/AcPowerSetPoint`) — drives the
  MultiPlus-II to import or export power, up to several kW, across all phases.
- **ESS minimum-SOC floor** (`/Settings/CGwacs/BatteryLife/MinimumSocLimit`) —
  lowered to `QW_MFRR_MIN_SOC` for mFRR dispatch only (never for a trade) and
  restored afterwards.

## Two kinds of event

| Kind    | What it is | Wire form | Actuated as |
|---------|------------|-----------|-------------|
| `frr`   | Balancing dispatch from the market (`fusebox`, `kratt`, or Qilowatt's own desk) | `_source` in `QW_MFRR_SOURCES`, `Mode` `frrup`/`frrdown` | export / import at `PowerLimit`, SOC floor lowered |
| `trade` | Qilowatt refilling (or emptying) the battery so the *next* dispatch can be delivered. The portal shows it as **Q** / BUY | `_source: qilowatt`, `Mode` `buy`/`sell`, `BatterySoc` = target | import / export at `PowerLimit`, floor untouched, ends when live SOC reaches the target |

Trades were dropped until 2026-09. Measured on site A (2026-07-14..09-14):
`qilowatt/buy` at 20–27 kW arrived 5–30 min after a `kratt/frrup` stand-down and
the next `frrup` followed 5–30 min later; 7 in July, 14 in August, 18 in the
first half of September. With the buy dropped, the battery sat at the mFRR floor
and the following up-regulation (paid ~1400 EUR/MWh) was short or not dispatched.
Honouring trades is a **site decision** with real costs — see the risks below —
and `QW_TRADE_MODES=` (empty) restores the old drop-everything behaviour.

**Risks specific to trades**

- *Battery cycling.* Each buy → frrup pair is a full charge/discharge swing;
  expect more cycles than mFRR alone.
- *Import cost.* A 27 kW buy for an hour is ~27 kWh at the day-ahead price. The
  vendor's economics assume the following activation pays for it; if a site is
  rarely dispatched after a buy, that assumption fails. Watch
  `afrr_probe.py`'s "followed by FRR" ratio.
- *DESS is off longer.* Arbitrage is suspended for the trade as well as the
  dispatch. `QW_MAX_TRADE_S` (default 5400 s) bounds it and must stay **below**
  the watchdog's `QW_MAX_OFF_SECS` for the same reason as `QW_MAX_EVENT_S`.
- *A sell respects the owner's floor.* The floor is not lowered for trades, so a
  `sell` stops at the arbitrage floor; the SOC-target end (`BatterySoc`) is the
  other bound. There is no evidence of Qilowatt sending `sell` to either site.
- *Only `buy`/`sell` can ever be actuated.* The set is fixed in code;
  `savebattery`/`limitexport`/`normal`/`pvsell`/`nobattery` from any source are
  dropped regardless of `QW_TRADE_MODES`.

A wrong setpoint, a stuck "off" state, or an uncontrolled export can trip
protection, exceed your grid connection capacity, or cause unwanted import/export
charges.

## Built-in safety layers

1. **Asymmetric setpoint clamp, twice** — the agent caps `|PowerLimit|` to
   `QW_MAX_IMPORT_W` / `QW_MAX_EXPORT_W` before dispatch, and `qw_grid_setpoint.sh`
   independently rejects any import (positive) `> QW_MAX_IMPORT_W` and any
   export (`|negative|`) `> QW_MAX_EXPORT_W` (both default 15000). The agent
   caps rather than rejects because a rejected write leaves the *previous*
   setpoint in place — a 27 kW `buy` against a 15 kW cap would otherwise deliver
   nothing. Set both to your site's grid-connection import capacity and feed-in
   (export) cap respectively; the agent reads them from the same env file.
2. **Atomic DESS save/restore** — `qw_dess_toggle.sh` saves the original DESS Mode
   before turning it off and restores exactly that value. The same save/restore
   covers the ESS minimum-SOC floor when `QW_MFRR_MIN_SOC` is set, with two
   guards: the floor is only ever lowered, never raised, and it is only restored
   if it still holds the value the script installed — so a floor change made in
   VRM during an event is kept rather than silently reverted. Trades call
   `off --no-floor`: a buy charges upward and a sell must respect the owner's
   floor. A dispatch arriving mid-trade re-runs plain `off`, which lowers the
   floor without touching the saved Mode.
3. **Watchdog** — `qw_dess_watchdog.sh`, run every minute, forces DESS back on if it
   has been off longer than `QW_MAX_OFF_SECS` (default 7800 s). This protects against
   a crashed agent leaving DESS off forever.
4. **State-machine failsafes** — the agent's Python state machine
   (`mfrr_statemachine.py`) releases the setpoint and restores DESS if the Qilowatt
   connection is lost for `QW_MQTT_LOST_FAILSAFE_S` (default 5 min) or an event runs
   longer than `QW_MAX_EVENT_S` (frr, default 2 h) / `QW_MAX_TRADE_S` (trade,
   default 1.5 h). A trade additionally ends when the live SOC reaches the
   commanded `BatterySoc`. On a clean stop the agent also reverts any active
   event. (The optional Node-RED flow carries an equivalent failsafe.)

   These limits and the watchdog's are a set and the ordering matters: both
   agent caps must stay **below** the watchdog's `QW_MAX_OFF_SECS`, so the agent
   is always the one that ends an event — releasing the grid setpoint first,
   then restoring DESS and the SOC floor. If the watchdog fires first, DESS and
   the arbitrage floor come back while the agent still holds the setpoint and
   believes the event is running. `tests/test_config.py` pins this ordering.

   Size `QW_MAX_EVENT_S` from your own logs rather than intuition. Both defaults
   were originally 1800 s, which real dispatch outgrew: across 179 site A events
   (2026-07-04 .. 07-26) the median was 620 s but 11 ran longer than 1800 s, the
   longest 6292 s, and the failsafe truncated 10 live events mid-delivery. To
   audit your own site:

   ```sh
   grep -c FAILSAFE /var/log/qw-agent/current /var/log/qw-agent/@*.s
   ```

   Every event end names its kind and trigger — `mFRR END (kratt/frrup 0 W)`,
   `mFRR END (failsafe: event > 7200s)`, `TRADE END (trade target reached: SOC
   100% >= 100%)` — because an unattributed end cannot be told apart from a
   failsafe, a stand-down, or a foreign automation, and attributing one after
   the fact means hand-matching timestamps across two logs.

   **A zero-power `frrup`/`frrdown` (or `buy`/`sell`) ends the event rather
   than holding 0 W.** It is the dispatcher standing down, and an event held at
   0 W keeps DESS off and the SOC floor lowered while delivering nothing. On
   site A these are routine — 121 in 24 days — and before this gate existed,
   52 of them stranded the site for a total of 4.4 h (median 4.8 min, worst
   12.5 min), every one ending only because an unrelated later command happened
   to arrive.

   **A dispatch during a trade (or vice versa) switches the event in place.**
   WorkMode is a single-state channel, so the newer command wins; the setpoint
   is rewritten without a DESS on/off cycle in between, because a few seconds
   of DESS-on would hand the inverter back to arbitrage mid-event.

5. **Connection watchdogs** — `ConnectionWatchdog` exits (so the supervisor restarts
   the agent with a fresh session) on three separate signs of deafness: the link
   reported down past `QW_LINK_RESTART_S`, the transport connected while the command
   topic stays unsubscribed past `QW_SUBSCRIBE_GRACE_S`, and no command at all for
   `QW_IDLE_REFRESH_S` while IDLE. The first two are cheap and reliable; the third
   is a last resort and the only one that can misfire.

   Size `QW_IDLE_REFRESH_S` above the site's longest genuine command silence, and
   note that setting it too low destroys the evidence needed to correct it: the
   portal pushes a snapshot ~20 s after every reconnect, so each refresh
   manufactures a command that resets the timer and the loop sustains itself.
   site B restarted every 6 h for weeks this way — 49 of 70 restarts — while its
   measured median gap simply mirrored the setting. At the next default (48 h)
   site A repeated it: over 72.6 days the probe found three silences of exactly
   48.0 h and none longer. The default is now 96 h, which clears every measured
   maximum (25.7 h, at least 31.9 h, and the 48.0 h artefact). Measure your own
   site rather than guessing, and treat a lower value as needing evidence:

   ```sh
   python3 /data/qw-agent/afrr_probe.py --log /data/afrr-workmode.log
   ```

   If you disable this backstop (`QW_IDLE_REFRESH_S=0`) to measure a site's real
   silence, note that nothing then restarts a deaf session. `qw_log_audit.sh`
   compensates by *reporting* command silence past `QW_HEALTH_MAX_SILENCE_H`
   (36 h) without restarting anything, so the site is still observable.

6. **Log audit** — `qw_log_audit.sh`, run hourly from the boot loop, reports new
   crashes, failsafe firings, watchdog restarts, dropped commands, a SOC floor
   that failed to engage and events ended by a foreign automation, to stdout and
   syslog (tag `qw_health`), exiting non-zero on any WARN/ERROR finding. Trade
   starts, SOC-target ends and capped requests are counted as INFO. A dropped
   `buy`/`sell` (`ignoring non-FRR Mode`) now means trades are disabled on that
   site, not "expected". It examines only lines added since its previous run, so
   a one-off event is reported once. Every defect this project has hit was
   visible in the log for weeks before anyone noticed, which is what this exists
   to fix.

## Operator responsibilities

- **Set `QW_MAX_IMPORT_W` / `QW_MAX_EXPORT_W` correctly** for the physical
  connection (import capacity and feed-in cap). Do not rely on the defaults.
- **Pick the right `QW_TELEMETRY_PROFILE`** (`dc_coupled` vs `ac_coupled`) so PV
  power is read from the correct dbus path.
- **Validate telemetry** (`SENSOR` payload) against your real system before relying
  on market settlement — incorrect telemetry can misrepresent available flexibility.
- **Dry-run first** (`QW_DRY_RUN=1`) and start with small values before going live.
- **Decide on Q trades per site** (`QW_TRADE_MODES`). The default honours
  `buy,sell`; a site that should only ever follow market dispatch sets it empty.
  After enabling, watch the first `TRADE START … → mFRR START` cycle in the log
  and the import on VRM, and check `afrr_probe.py`'s "followed by FRR" ratio
  after a week.
- **Keep the watchdog running** at all times (boot loop). It is the last line of
  defence.
- **Check that the two-level SOC floor actually engages** if you set
  `QW_MFRR_MIN_SOC`: it must be below the dashboard's Minimum SOC slider, and the
  log says `Lowered SOC floor X% -> Y%` when it works and `nothing to lower` when
  it does not.
- **Beware the decoy floor register.** Some sites also carry
  `/Settings/DynamicEss/MinSoc`, which looks like the DESS floor but is not the one
  this build honours — verify with `/Control/ActiveSocLimit`, which follows
  `MinimumSocLimit`. Where the decoy exists, keep it equal to the dashboard slider
  so nobody later reads it as the live floor. Aligning it that way is a no-op
  whether the firmware ignores the register or ever starts honouring it; deleting
  it is not, because the value it would be recreated with is unknown.
- **Never run two orchestrators at once** (e.g. an old HA automation, the agent's
  state machine, and a Node-RED actuator flow) — they write the same dbus paths and
  will race.
- **The second orchestrator can be in the vendor's cloud.** On 2026-07-27
  Qilowatt's Energy Optimizer was enabled on both sites; it writes the same
  WorkMode channel as the mFRR dispatcher, on 15-minute slot boundaries, and it
  ended every event on both sites that morning. Its Modes are never actuated
  here — arbitrage is Victron DESS's job — so it contributed nothing and only
  truncated delivery. If a site runs DESS for arbitrage, the cloud-side
  optimiser is redundant by construction. Note the coupling risk before
  disabling it: the vendor documents the Optimizer as the component that
  manages mFRR signals, so confirm dispatch survives rather than assuming it.

## Emergency stop

To immediately return the system to normal:

```sh
/data/qw_dess_toggle.sh on
/data/qw_grid_setpoint.sh 0
```

Then stop the agent service (`svc -d /service/qw-agent`) — its clean shutdown
already reverts any active event — and disable the Node-RED flow tab if present.

## Disclaimer

Provided "as is", without warranty. See [LICENSE](../LICENSE). You are responsible
for safe operation, compliance with your grid code and connection agreement, and any
aggregator/market obligations.
