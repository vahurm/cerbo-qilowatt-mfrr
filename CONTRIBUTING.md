# Contributing

Thanks for helping make this run on more Victron sites. A few rules keep it
safe — it commands real grid power.

## Ground rules

1. **Every behaviour change ships with a test.** `pytest -q` for Python,
   `sh tests/test_*.sh` for the scripts. CI runs both on Python 3.8/3.10/3.12
   plus shellcheck. `make test` runs everything locally.
2. **Shell scripts are POSIX `sh`** (Venus OS is BusyBox: no bash, no
   `timeout`, no `pgrep`, no `[[ ]]`). `make lint` runs shellcheck with
   `-s sh`.
3. **Do not widen the actuated Mode sets.** `FRR_MODES = (frrup, frrdown)`
   and `TRADE_MODES = (buy, sell)` in `agent/mfrr_statemachine.py` are the
   hard guard that keeps `savebattery`/`limitexport`/`normal` — and whatever
   the vendor adds next — out of the actuators. A new Mode needs a documented
   measurement of what it means on the wire before it is honoured.
4. **Never add `optimizer` to a default source list.** It is Qilowatt's
   arbitrage scheduler writing the same WorkMode channel; listing it hands the
   actuators to a second orchestrator (SAFETY.md).
5. **The local-bridge contract is frozen.** Topic names and payloads in
   `tests/test_local_bridge_contract.py` are consumed by live sites'
   Node-RED flows. Add topics; do not rename or change payloads.
6. **Caps ordering is invariant**: `QW_MAX_EVENT_S`, `QW_MAX_TRADE_S` <
   `QW_MAX_OFF_SECS` (watchdog). Tests, `startup.config_warnings` and
   `qw_doctor.sh` all pin it; change all three together.
7. **No secrets, no site identifiers.** `.env` files are ignored; docs refer to
   the reference sites as "site A" / "site B".
8. **Numbers in comments come from logs.** When you change a default (cap,
   timeout, refresh), say what was measured and where, like the existing
   comments do. `tools/afrr_probe.py` and `tools/replay.py` exist for that.

## Workflow

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r agent/requirements.txt -r requirements-dev.txt
make test          # pytest + all sh tests
make lint          # shellcheck (pip install shellcheck-py if missing)
```

- One logical change per commit; conventional prefixes (`feat:`, `fix:`,
  `docs:`, `test:`, `ops:`) as in `git log`.
- If the change affects what runs on the Cerbo, say in the PR whether it was
  exercised with `install.sh --dry-run-window` on real hardware.
- Update `CHANGELOG.md` under an "Unreleased" heading.

## Releasing

1. Bump `__version__` in `agent/qw_agent.py`, move "Unreleased" to a dated
   section in `CHANGELOG.md`.
2. `git tag -a vX.Y.Z -m "..." && git push --tags`.
3. Optionally `make release` and attach `build/pylib.tar.gz` to the GitHub
   release so users without pip can `install.sh --pylib-tarball`.
