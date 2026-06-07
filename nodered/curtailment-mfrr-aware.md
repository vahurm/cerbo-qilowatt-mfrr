# Making a co-resident curtailment flow mFRR-aware

If the same Cerbo also runs an AC-coupled PV **curtailment** flow (e.g. a Huawei
inverter that follows Dynamic ESS intent via Modbus), it must be made aware of
mFRR events. Otherwise the two flows fight:

- During an mFRR event the state machine turns **DESS off** (`Mode=0`).
- A DESS-following curtailment loop sees `DynamicEss/Active = 0` and forces the PV
  toward **zero export** (conservative fallback).
- That cancels most of the **frrup** (export) capacity you are being paid to deliver.

The mFRR flow in [`flow.json`](flow.json) publishes a shared Node-RED **global**
flag at every transition:

```javascript
global.set('qw_mfrr', { active: true,  mode: 'frrup',  signed_watts: -5000, ts: <ms> });
// ... and on release / failsafe:
global.set('qw_mfrr', { active: false, mode: '', signed_watts: 0, ts: <ms> });
```

Because both flows run in the same Node-RED instance, they share `global` context
— no extra wiring or MQTT needed.

## Patch the curtailment `decide_fn`

Add this near the **top** of the curtailment decision function, before the normal
DESS-following logic:

```javascript
// --- mFRR coexistence ---
const mfrr = global.get('qw_mfrr') || { active: false };
if (mfrr.active) {
    let targetPct;
    if (mfrr.mode === 'frrup') {
        // Export event: let the AC-coupled PV run flat out so it contributes
        // maximum export capacity. The MP-II grid setpoint pulls the rest.
        targetPct = 100;
    } else {
        // frrdown (import) or other: cover local demand normally. Do NOT apply
        // the DESS-off "hold export at 0" fallback — DESS is intentionally off.
        const exportW        = Math.max(0, -gridW);
        const importW        = Math.max(0,  gridW);
        const battDischargeW = Math.max(0,  battW);
        if (battDischargeW > TOLERANCE_W || importW > TOLERANCE_W) {
            targetPct = Math.round((huaweiW + battDischargeW + importW) / PMAX_W * 100);
        } else {
            targetPct = lastPct;
        }
    }
    flow.set('last_target_pct', targetPct);
    msg.payload = { reason: 'mfrr_' + (mfrr.mode || 'active'), target_pct: targetPct };
    // ... fall through to your existing Modbus-write branch with this targetPct
    return msg;   // bypass the normal dessOk / AllowGridFeedIn logic
}
// --- end mFRR coexistence; normal DESS-following logic continues below ---
```

When the event ends (`active: false`), the next cycle resumes normal
DESS-following automatically.

## Failsafe interaction

The mFRR state machine clears the flag (`active: false`) on **every** exit path,
including the `mqtt_lost` and `max_duration` failsafes. So if the agent or the
cloud link dies mid-event, the curtailment flow returns to its safe
DESS-following behaviour within one cycle, and the DESS watchdog
(`qw_dess_watchdog.sh`) independently forces DESS back on.

## Notes

- `TOLERANCE_W`, `PMAX_W`, `lastPct`, `huaweiW`, `gridW`, `battW` are the variables
  already present in a typical curtailment `decide_fn`; adapt names to your flow.
- Keep the curtailment flow's own stale-write detector and conservative fallback
  for everything **outside** an mFRR event.
