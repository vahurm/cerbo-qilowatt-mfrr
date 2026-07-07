# aFRR delivery verification

Qilowatt [opened the aFRR market](https://qilowatt.eu/afrr/) and states that for
customers on a **Qilowatt hardware controller** (Modbus R2 etc.) it is automatic
— "same contract, same controller, nothing to configure". This repo is **not**
that: it is a custom Python agent that impersonates a Qilowatt inverter via
`qilowatt-py` and implements an **mFRR-only** state machine
([`agent/mfrr_statemachine.py`](../agent/mfrr_statemachine.py), acts only on
`_source in {fusebox, kratt}` + `Mode in {frrup, frrdown}`).

So the "nothing to do" promise does **not** automatically apply here. This is a
**verify-only** procedure: observe what actually arrives, classify it, and
decide. It changes **no** agent behaviour.

## Why this is not obvious from the docs

`qilowatt-py`'s [`WorkModeCommand`](https://github.com/qilowatt/qilowatt-py/blob/main/src/qilowatt/models.py)
has no aFRR-specific field — only `Mode` / `_source` / `PowerLimit` (+ an
`extras` catch-all). aFRR is described as a *continuous, closed-loop* signal,
which is very different from mFRR block dispatch. Three outcomes are possible and
we cannot tell which is true without live data:

| Outcome | What we would observe | Impact on this agent |
|---|---|---|
| **Unaffected** | No new WORKMODE traffic; aFRR closed loop runs vendor-side | Agent keeps doing mFRR only; no aFRR revenue |
| **Safe-but-idle** | New `_source`/`Mode`/extra key the agent doesn't match | Agent silently ignores aFRR (no harm, no revenue) |
| **Risky** | `kratt` FRR setpoints at a fast, continuous cadence | Block-event logic toggles DESS every activation and thrashes |

## Step 1 — Capture (read-only)

The agent already logs every command as `WORKMODE received: {...}` at **INFO**
(see `on_command` in [`agent/qw_agent.py`](../agent/qw_agent.py)), so **no code
change and no DEBUG are needed to observe** — DEBUG only adds noise and shortens
the multilog ring (`/var/log/qw-agent`, ~10 files, a few days of history).

For a capture window longer than the ring, use the durable read-only tap
([`tools/afrr_capture.sh`](../tools/afrr_capture.sh)). It appends only WORKMODE
lines to `/data/afrr-workmode.log` (survives rotation and reboots; busybox-safe,
uses `awk` since `grep --line-buffered` is absent on Venus OS):

```sh
# on the Cerbo (deploy/install.sh installs it; or scp it to /data)
/data/afrr_capture.sh start     # seeds history, then follows in background
/data/afrr_capture.sh status    # pid + captured line count
```

Optional passive MQTT sniff (a *second* subscribe-only session; see the caveat
in the tool header before using it):

```sh
python3 tools/afrr_probe.py --live --capture-file /data/afrr-capture.jsonl
```

## Step 2 — Classify

Run the read-only classifier ([`tools/afrr_probe.py`](../tools/afrr_probe.py))
over the captured log. It reports `_source` / `Mode` / `PowerLimit` / `extras`
and the FRR cadence, then prints a fingerprint and a decision:

```sh
# offline over the durable capture (recommended)
python3 /data/qw-agent/afrr_probe.py --log /data/afrr-workmode.log

# or over the multilog ring (archived rotations + current)
cat /var/log/qw-agent/@*.s /var/log/qw-agent/current | python3 /data/qw-agent/afrr_probe.py

# or follow live
tail -F /var/log/qw-agent/current | python3 /data/qw-agent/afrr_probe.py
```

The aFRR fingerprint it looks for:

- **new `_source`/`Mode` or an extra WORKMODE key** -> `unrecognized_signal` -> SAFE-BUT-IDLE
- **`kratt` FRR with a short median interval + changing `PowerLimit`** ->
  `continuous_modulation` -> RISKY (tune `--continuous-interval`, default 30 s)
- **only sparse `kratt`/`fusebox` FRR blocks** -> `block_mfrr` -> UNAFFECTED
- **nothing FRR/unknown** -> `inconclusive` -> capture longer

## Step 3 — Cross-check

- Correlate the capture timestamps with aFRR activations shown in the Qilowatt
  app (it marks aFRR as separate events) to confirm what the fingerprint means.
- Ask Qilowatt / KratTrade support directly:
  1. Does aFRR reach **software** devices (`qilowatt-py` / `qilowatt-ha`, i.e. no
     Modbus R2), or only their hardware controllers?
  2. If yes, via which `Mode` / `_source` on `Q/<id>/cmnd/backlog`, and what is
     the update cadence?
  3. Is the frequency closed-loop expected **device-side** (we must follow grid
     frequency locally) or **cloud-side** (they stream us setpoints)?

## Step 4 — Decide

### Kungla (einstein, Venus OS v3.75) — recorded 2026-07-07

```
Capture window : ~2026-07-03 .. 2026-07-07 (multilog ring, ~4 days)
Site           : Kungla (dc_coupled), device Q/d2974750-…/  agent up 3.5 d
Sample size    : 242 WORKMODE commands
Sources        : kratt=216, notimer=26        (no unknown sources)
Modes          : frrup=192, frrdown=24, normal=26   (no unknown modes)
Extra keys     : NONE   (nothing lands in qilowatt-py's `extras`)
FRR cadence    : median 59.4 s between commands, 211 setpoint changes
Probe verdict  : block_mfrr -> UNAFFECTED
DESS behaviour : toggles OFF once per event, ON at return-to-normal
                 (notimer); setpoint updates do NOT re-toggle -> no thrash
```

**Chosen bucket: UNAFFECTED (no agent change needed).**

Interpretation: every market signal on this device arrives through the same
`kratt` WORKMODE stream that the agent already acts on. No new `_source`/`Mode`
and no new WORKMODE field appeared for aFRR. Cadence is ~1/min (more dynamic than
classic mFRR blocks, consistent with KratTrade's portfolio) but far from the
sub-10 s continuous regulation that would trip the `continuous_modulation`
fingerprint — so the block-event state machine is not at risk of DESS thrash
(confirmed in the logs). Practically, "nothing to do" holds for this custom
agent too: it is already processing the whole KratTrade stream.

### Remaining human confirmation (cannot be done from the agent)

Ask Qilowatt / KratTrade to close the one ambiguity the stream cannot resolve:
is true continuous aFRR regulation expected **device-side** (only on the Modbus
R2 hardware) or is it **blended into the `kratt` cloud dispatch** we already act
on? The durable capture (`/data/afrr_capture.sh`, boot-persistent) keeps
accruing, so if a faster/continuous or new-typed signal ever appears the probe
will flip the verdict to RISKY / SAFE-BUT-IDLE on the next run:

```sh
python3 /data/qw-agent/afrr_probe.py --log /data/afrr-workmode.log
```

### Buckets (reference)

- **UNAFFECTED** — no aFRR fingerprint on the WORKMODE stream; no code change.
- **SAFE-BUT-IDLE** — aFRR arrives with an unknown source/mode/extra key the
  agent ignores; open a follow-up to participate.
- **RISKY** — aFRR arrives as continuous `kratt` setpoints; prioritise an
  adaptation (continuous-setpoint handling, DESS-thrash guard, failsafe retune).
