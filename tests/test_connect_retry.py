"""Tests for connect_with_retry — surviving a transient resolve failure at boot.

Venus OS starts the service before the network is reliably up. Kungla crashed on
``socket.gaierror`` from the initial connect roughly twice a day (47 of 50
restarts in one log ring), so the first connect gets a bounded backoff before we
give up and let the supervisor take over.
"""

from __future__ import annotations

import socket

import pytest

import qw_agent


class _Client:
    """Fails the first ``fail_times`` connects, then succeeds."""

    def __init__(self, fail_times: int, exc: BaseException | None = None) -> None:
        self._fail_times = fail_times
        self._exc = exc or socket.gaierror(-3, "Temporary failure in name resolution")
        self.calls = 0

    def connect(self) -> None:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc


def _recorder():
    slept: list[float] = []
    return slept, slept.append


def test_first_attempt_success_does_not_sleep():
    client = _Client(fail_times=0)
    slept, sleep = _recorder()
    qw_agent.connect_with_retry(client, attempts=5, retry_s=5.0, sleep=sleep)
    assert client.calls == 1
    assert slept == []


def test_transient_failure_is_retried_with_doubling_backoff():
    client = _Client(fail_times=2)
    slept, sleep = _recorder()
    qw_agent.connect_with_retry(client, attempts=5, retry_s=5.0, sleep=sleep)
    assert client.calls == 3
    assert slept == [5.0, 10.0]


def test_persistent_failure_raises_so_the_supervisor_takes_over():
    client = _Client(fail_times=99)
    slept, sleep = _recorder()
    with pytest.raises(socket.gaierror):
        qw_agent.connect_with_retry(client, attempts=3, retry_s=5.0, sleep=sleep)
    # One sleep between attempts, none after the last one.
    assert client.calls == 3
    assert slept == [5.0, 10.0]


def test_backoff_is_capped():
    client = _Client(fail_times=99)
    slept, sleep = _recorder()
    with pytest.raises(OSError):
        qw_agent.connect_with_retry(client, attempts=8, retry_s=20.0, sleep=sleep)
    assert max(slept) == 60.0
    assert slept == [20.0, 40.0, 60.0, 60.0, 60.0, 60.0, 60.0]


def test_single_attempt_fails_immediately():
    client = _Client(fail_times=1)
    slept, sleep = _recorder()
    with pytest.raises(socket.gaierror):
        qw_agent.connect_with_retry(client, attempts=1, retry_s=5.0, sleep=sleep)
    assert client.calls == 1
    assert slept == []


def test_non_os_errors_are_not_retried():
    class _Boom(Exception):
        pass

    client = _Client(fail_times=1, exc=_Boom("bad credentials"))
    slept, sleep = _recorder()
    with pytest.raises(_Boom):
        qw_agent.connect_with_retry(client, attempts=5, retry_s=5.0, sleep=sleep)
    assert client.calls == 1
    assert slept == []
