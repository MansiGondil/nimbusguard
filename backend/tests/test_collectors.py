from datetime import UTC, datetime

import pytest

from app.collectors import netflow, syslog
from app.collectors.base import Flow
from app.collectors.cloud import CloudWatchCollector
from app.collectors.simulator import NetworkSimulator, SimDevice
from app.collectors.snmp import SnmpPoller


def test_syslog_rfc3164():
    e = syslog.parse(b"<38>Oct 11 22:14:15 fw-edge-01 sshd[22]: Failed password for root from 45.155.205.17 port 22")
    assert e.host == "fw-edge-01" and e.severity == 6 and e.facility == 4
    assert e.category == "auth" and e.source_ip == "45.155.205.17"


def test_syslog_rfc5424_and_interface_category():
    e = syslog.parse(
        "<187>1 2026-10-28T10:00:00Z sw-dc-01 ios - - - %LINK-3-UPDOWN: Interface Gi0/1, changed state to down"
    )
    assert e.host == "sw-dc-01" and e.severity == 3
    assert e.time == datetime(2026, 10, 28, 10, tzinfo=UTC)
    assert e.category == "interface"


def test_syslog_without_header_uses_source():
    e = syslog.parse(b"Configuration saved by admin", source_ip="10.0.0.5")
    assert e.host == "10.0.0.5" and e.category == "config"


def test_netflow_v5_roundtrip():
    flows = [
        Flow(
            datetime.now(UTC),
            "10.1.1.99",
            "185.220.101.5",
            bytes=250_000_000,
            packets=180_000,
            src_port=51515,
            dst_port=443,
            protocol=6,
        ),
        Flow(datetime.now(UTC), "10.1.2.3", "8.8.8.8", bytes=1200, packets=4, src_port=5353, dst_port=53, protocol=17),
    ]
    packet = netflow.encode_v5(flows, unix_secs=1_800_000_000)
    assert len(packet) == 24 + 2 * 48
    decoded = netflow.decode_v5(packet)
    assert [(f.src_ip, f.dst_ip, f.bytes, f.dst_port, f.protocol) for f in decoded] == [
        ("10.1.1.99", "185.220.101.5", 250_000_000, 443, 6),
        ("10.1.2.3", "8.8.8.8", 1200, 53, 17),
    ]
    with pytest.raises(ValueError):
        netflow.decode_v5(packet[:40])


async def test_snmp_counters_become_rates():
    readings = iter(
        [
            {
                "cpu_idle": 80,
                "mem_total": 4_000_000,
                "mem_avail": 1_000_000,
                "in_octets": 0,
                "out_octets": 0,
                "in_errors": 10,
            },
            {
                "cpu_idle": 55,
                "mem_total": 4_000_000,
                "mem_avail": 1_000_000,
                "in_octets": 750_000_000,
                "out_octets": 375_000_000,
                "in_errors": 70,
            },
        ]
    )
    poller = SnmpPoller()
    first = poller.convert("r1", next(readings), 1000, now=0.0)
    assert first == {"cpu": 20.0, "memory": 75.0}  # rates need two polls
    second = poller.convert("r1", next(readings), 1000, now=60.0)
    assert second["cpu"] == 45.0
    assert second["traffic_in"] == 100.0  # 750 MB in 60 s = 100 Mbps
    assert second["traffic_out"] == 50.0
    assert second["if_errors"] == 60.0  # per minute


async def test_snmp_poll_uses_fetcher_and_profile():
    async def fake_fetch(host, port, community, oids):
        assert community == "s3cret" and host == "10.0.0.1"
        return {
            "1.3.6.1.4.1.9.9.109.1.1.1.1.8.1": 33.0,
            "1.3.6.1.4.1.9.9.48.1.1.1.5.1": 300.0,
            "1.3.6.1.4.1.9.9.48.1.1.1.6.1": 700.0,
        }

    sample = await SnmpPoller(fake_fetch).poll("r1", "10.0.0.1", "s3cret", "cisco-ios")
    assert sample.values["cpu"] == 33.0 and sample.values["memory"] == 30.0 and "latency" in sample.values


async def test_cloudwatch_mapping():
    class Stub:
        def get_metric_data(self, **kw):
            assert kw["MetricDataQueries"][0]["MetricStat"]["Metric"]["Dimensions"][0]["Value"] == "i-123"
            return {
                "MetricDataResults": [{"Id": "cpu", "Values": [42.5]}, {"Id": "traffic_in", "Values": [3_750_000_000]}]
            }

    sample = await CloudWatchCollector(client=Stub()).collect("c1", {"instance_id": "i-123"})
    assert sample.values == {"cpu": 42.5, "traffic_in": 100.0}


def _sim():
    devs = [
        SimDevice("core", "core", "router", "10.0.0.1"),
        SimDevice("sw", "sw", "switch", "10.0.1.1"),
        SimDevice("ap", "ap", "access_point", "10.0.2.1", 300),
        SimDevice("fw", "fw", "firewall", "10.0.0.2"),
    ]
    return NetworkSimulator(devs, [("core", "sw"), ("sw", "ap")], seed=1)


def test_simulator_faults_propagate_and_resolve():
    sim = _sim()
    t = datetime(2026, 1, 5, 10, tzinfo=UTC)
    before = sim.sample("ap", t)["latency"]
    sim.inject("bad_cable", "sw", now=t)
    later = t.replace(minute=5)
    assert sim.sample("sw", later)["if_errors"] > 30
    assert sim.sample("ap", later)["latency"] > before + 5  # downstream symptom
    assert sim.execute("switch_to_backup_port", "sw", {})["resolved_faults"] == ["bad_cable"]
    assert not sim.faults


def test_simulator_device_down_hides_dependents_and_stubborn_faults():
    sim = _sim()
    t = datetime(2026, 1, 5, 10, tzinfo=UTC)
    sim.inject("device_down", "sw", now=t)
    assert {s.device_id for s in sim.tick(t).samples} == {"core", "fw"}
    sim.clear()
    sim.inject("brute_force", "fw", now=t, stubborn=True)
    assert any(e.category == "auth" for e in sim.tick(t).logs)
    assert "resolved_faults" not in sim.execute("block_ip", "fw", {})
    assert sim.faults
