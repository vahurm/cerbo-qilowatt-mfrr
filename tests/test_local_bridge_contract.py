"""Pins the local MQTT bridge contract (topics + payloads).

Site B's Node-RED curtailment flow consumes ``qw/mfrr_active`` (``on``/``off``)
and ``qw/online`` (retained ``true``/``false``, LWT). Renaming a topic or
changing a payload silently breaks a live site, so the contract is a test.
Also asserted: everything is published retained, and the mFRR state is
republished on every QW (re)connect so a retained baseline always exists.
"""

from __future__ import annotations

import types

import pytest

import qw_agent
from conftest import Command


class _FakeMqttClient:
    def __init__(self, *a, **kw):
        self.published: list = []
        self.will = None
        self.on_connect = None

    def username_pw_set(self, *a):
        pass

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def reconnect_delay_set(self, **kw):
        pass

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, str(payload), retain))

    def connect_async(self, *a, **kw):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(qw_agent.mqtt, "Client", _FakeMqttClient)
    cfg = types.SimpleNamespace(
        prefix="qw", local_user=None, local_pass=None, local_host="127.0.0.1", local_port=1883
    )
    b = qw_agent.LocalBridge(cfg)
    return b, b._client


def _last(published, topic):
    return [p for t, p, _ in published if t == topic][-1]


def test_online_topic_lwt_and_connect_payloads(bridge):
    b, client = bridge
    assert client.will == ("qw/online", "false", True)
    client.on_connect(client, None, None, 0)
    assert client.published[-1] == ("qw/online", "true", True)
    b.stop()
    assert client.published[-1] == ("qw/online", "false", True)


def test_mfrr_topics_and_payloads(bridge):
    b, client = bridge
    b.publish_mfrr("ACTIVE", -5000, "frr")
    pub = client.published
    assert _last(pub, "qw/mfrr_active") == "on"
    assert _last(pub, "qw/mfrr_kind") == "frr"
    assert _last(pub, "qw/mfrr_signed_w") == "-5000"
    assert _last(pub, "qw/mfrr_degraded") == "false"

    b.publish_mfrr("IDLE", 0, None, degraded=True)
    assert _last(pub, "qw/mfrr_active") == "off"
    assert _last(pub, "qw/mfrr_kind") == "none"
    assert _last(pub, "qw/mfrr_signed_w") == "0"
    assert _last(pub, "qw/mfrr_degraded") == "true"


def test_trade_is_reported_active_with_kind_trade(bridge):
    b, client = bridge
    b.publish_mfrr("ACTIVE", 20000, "trade")
    assert _last(client.published, "qw/mfrr_active") == "on"
    assert _last(client.published, "qw/mfrr_kind") == "trade"


def test_workmode_and_connection_topics(bridge):
    b, client = bridge
    b.publish_workmode(Command(_source="kratt", Mode="frrup", PowerLimit=12000))
    b.publish_connected(True)
    pub = client.published
    assert _last(pub, "qw/qw_source") == "kratt"
    assert _last(pub, "qw/qw_mode") == "frrup"
    assert _last(pub, "qw/qw_powerlimit") == "12000"
    assert _last(pub, "qw/qw_connected") == "on"
    b.publish_connected(False)
    assert _last(pub, "qw/qw_connected") == "off"


def test_everything_is_retained(bridge):
    b, client = bridge
    b.publish_mfrr("ACTIVE", -1, "frr")
    b.publish_workmode(Command(_source="kratt", Mode="frrup", PowerLimit=1))
    b.publish_connected(True)
    client.on_connect(client, None, None, 0)
    assert all(retain for _, _, retain in client.published)


def test_topic_set_is_exactly_the_documented_one(bridge):
    b, client = bridge
    b.publish_mfrr("ACTIVE", -1, "frr")
    b.publish_workmode(Command(_source="kratt", Mode="frrup", PowerLimit=1))
    b.publish_connected(True)
    client.on_connect(client, None, None, 0)
    assert {t for t, _, _ in client.published} == {
        "qw/online",
        "qw/qw_connected",
        "qw/qw_source",
        "qw/qw_mode",
        "qw/qw_powerlimit",
        "qw/mfrr_active",
        "qw/mfrr_kind",
        "qw/mfrr_signed_w",
        "qw/mfrr_degraded",
    }
