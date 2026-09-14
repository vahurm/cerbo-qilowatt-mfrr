---
name: Bug report / site does not work
about: The agent misbehaves, or does nothing, on your Cerbo
title: ''
labels: ''
assignees: ''
---

**What happened / what you expected**

<!-- One or two sentences. If a command was or was not acted on, say which. -->

**`/data/qw_doctor.sh` output** (run on the Cerbo; it prints no credentials)

```
paste here
```

**Agent log** — `tail -n 300 /var/log/qw-agent/current`

```
paste here
```

**Version** — `cat /data/qw-agent/VERSION` (or the `Starting qw_agent ...` log line)

```
```

**Hardware / firmware**

- GX device and Venus OS version:
- Inverter/charger model(s) and phases:
- PV topology (DC MPPT / AC on output / AC on grid / mixed):
- Grid meter (which) or Multi-measured:
- Dispatcher(s) seen in `qw_source` (`kratt`, `fusebox`, `qilowatt`, …):

**Optional** — `python3 tools/replay.py --log <the log above> --timeline`

```
```

**Checklist**

- [ ] `.env` has no `REPLACE_WITH_*` left (doctor says PASS)
- [ ] I have not pasted `QW_MQTT_PASS` or any credential above
- [ ] No other orchestrator (Home Assistant, legacy Node-RED flow) writes `AcPowerSetPoint` / `DynamicEss/Mode` on this site
