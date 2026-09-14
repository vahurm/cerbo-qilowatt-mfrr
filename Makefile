## cerbo-qilowatt-mfrr — developer shortcuts
##
##   make test        run every Python and POSIX-sh test
##   make lint        shellcheck the sh scripts (if shellcheck is installed)
##   make release     build build/pylib.tar.gz (vendored pure-Python deps) so a
##                    workstation without pip can deploy with
##                    deploy/install.sh --pylib-tarball build/pylib.tar.gz
##   make replay LOG=/path/afrr-workmode.log   what WOULD the agent have done

PYTHON ?= python3
SH_SCRIPTS := scripts/*.sh tools/afrr_capture.sh deploy/install.sh service/qw-agent/run service/qw-agent/log/run
SH_TESTS := tests/test_grid_setpoint.sh tests/test_dess_toggle.sh tests/test_log_audit.sh tests/test_doctor.sh

.PHONY: test pytest shtest lint release replay clean

test: pytest shtest

pytest:
	$(PYTHON) -m pytest -q

shtest:
	@for t in $(SH_TESTS); do echo "== $$t"; sh $$t >/dev/null || { sh $$t; exit 1; }; done; echo "shell tests OK"

lint:
	@command -v shellcheck >/dev/null || { echo "shellcheck not installed"; exit 0; }
	shellcheck -S warning -s sh -e SC2039,SC3043 scripts/*.sh tools/afrr_capture.sh
	shellcheck -S warning -s bash deploy/install.sh

release:
	rm -rf build/pylib && mkdir -p build/pylib
	$(PYTHON) -m pip install --quiet --target build/pylib -r agent/requirements.txt
	tar -czf build/pylib.tar.gz -C build/pylib .
	@echo "built build/pylib.tar.gz ($$(du -h build/pylib.tar.gz | cut -f1))"

replay:
	@test -n "$(LOG)" || { echo "usage: make replay LOG=/path/to/afrr-workmode.log"; exit 2; }
	$(PYTHON) tools/replay.py --log "$(LOG)"

clean:
	rm -rf build .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
