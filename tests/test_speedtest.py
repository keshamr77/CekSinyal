import json
import socket
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
import requests

from ceksinyal import db, netif
from ceksinyal.__main__ import main
from ceksinyal.config import Config, DeviceConfig, SpeedtestConfig, load_config
from ceksinyal.speedtest import METHOD_FULL, METHOD_LIGHT, SpeedResult
from ceksinyal.speedtest import full as full_mod
from ceksinyal.speedtest import light as light_mod
from ceksinyal.speedtest.runner import SpeedtestRunner

T0 = datetime(2026, 11, 15, 8, 0, 0, tzinfo=timezone.utc)
IP = "192.168.42.129"
ST = SpeedtestConfig()


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += timedelta(minutes=minutes)


class Ticker:
    """Jam palsu untuk perf_counter: maju `step` detik tiap dipanggil."""

    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


# ------------------------------------------------------------ ping TCP

class FakeSock:
    closed = False

    def close(self):
        self.closed = True


def test_tcp_ping_binds_source_and_returns_median():
    calls = []

    def connect(addr, timeout, source_address):
        calls.append((addr, timeout, source_address))
        return FakeSock()

    ms = light_mod.tcp_ping("1.1.1.1", 443, IP, 3, 5, connect=connect, clock=Ticker(0.02))
    assert ms == 20.0
    assert calls == [(("1.1.1.1", 443), 5, (IP, 0))] * 3


def test_tcp_ping_partial_failure_and_total_failure():
    outcomes = [OSError("timeout"), FakeSock(), OSError("timeout")]

    def flaky(addr, timeout, source_address):
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    assert light_mod.tcp_ping("h", 443, None, 3, 5, connect=flaky, clock=Ticker(0.05)) == 50.0

    def dead(addr, timeout, source_address):
        raise socket.timeout("timed out")

    with pytest.raises(OSError):
        light_mod.tcp_ping("h", 443, IP, 3, 5, connect=dead)


# ------------------------------------------------------------ unduhan

class FakeResponse:
    def __init__(self, chunks, status=200):
        self.chunks, self.status = chunks, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} Error")

    def iter_content(self, size):
        yield from self.chunks


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.closed, self.calls = response, error, False, []

    def get(self, url, stream, timeout):
        self.calls.append((url, stream, timeout))
        if self.error:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


def test_download_computes_mbps():
    session = FakeSession(FakeResponse([b"x" * 250_000] * 4))  # 1 MB
    seen = []

    def factory(ip):
        seen.append(ip)
        return session

    # jam maju 0,2 detik per panggilan: mulai, 4 pengecekan, selesai -> 1,0 detik
    mbps = light_mod.download("http://u", IP, 30, session_factory=factory, clock=Ticker(0.2))
    assert mbps == 8.0 and seen == [IP] and session.closed
    assert session.calls == [("http://u", True, 30)]


def test_download_stops_at_time_limit_and_uses_partial_bytes():
    session = FakeSession(FakeResponse([b"x" * 100_000] * 50))
    mbps = light_mod.download("http://u", IP, 10, session_factory=lambda ip: session, clock=Ticker(4))
    # potongan ke-3 melewati batas 10 detik: 300 kB dalam 16 detik
    assert mbps == 0.15


def test_download_errors():
    with pytest.raises(ValueError, match="terlalu kecil"):
        light_mod.download("http://u", IP, 10, session_factory=lambda ip: FakeSession(FakeResponse([b"x" * 10])))
    with pytest.raises(requests.HTTPError):
        light_mod.download("http://u", IP, 10, session_factory=lambda ip: FakeSession(FakeResponse([], 503)))
    with pytest.raises(requests.ConnectionError):
        light_mod.download("http://u", IP, 10, session_factory=lambda ip: FakeSession(error=requests.ConnectionError("x")))


def test_session_bound_to_source_ip():
    session = light_mod.make_session(IP)
    adapter = session.get_adapter("https://example.com")
    assert isinstance(adapter, light_mod.SourceAddressAdapter)
    assert adapter.poolmanager.connection_pool_kw["source_address"] == (IP, 0)
    assert session.trust_env is False
    session.close()


# ------------------------------------------------------------ uji ringan

def test_run_light_ok():
    r = light_mod.run_light(ST, IP, ping=lambda *a: 31.5, fetch=lambda *a: 12.3)
    assert (r.method, r.source_ip, r.ping_ms, r.download_mbps, r.upload_mbps, r.ok, r.error) == (
        "light", IP, 31.5, 12.3, None, True, None)


def test_run_light_download_fails():
    def boom(*a):
        raise requests.ConnectionError("jaringan putus")

    r = light_mod.run_light(ST, IP, ping=lambda *a: 31.5, fetch=boom)
    assert not r.ok and r.ping_ms == 31.5 and r.download_mbps is None and "unduh gagal: jaringan putus" in r.error


def test_run_light_ping_fails_but_download_ok():
    def boom(*a):
        raise OSError("timed out")

    r = light_mod.run_light(ST, IP, ping=boom, fetch=lambda *a: 5.0)
    assert r.ok and r.ping_ms is None and r.error == "ping gagal: timed out"


# ------------------------------------------------------------ uji penuh

def _proc(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def test_run_full_parses_json_and_passes_source():
    seen = {}

    def runner(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return _proc(json.dumps({"download": 25_400_000.0, "upload": 8_120_000.0, "ping": 23.456}))

    r = full_mod.run_full(ST, IP, runner=runner)
    assert (r.method, r.ok, r.ping_ms, r.download_mbps, r.upload_mbps, r.error) == ("full", True, 23.5, 25.4, 8.12, None)
    assert seen["cmd"][1:] == ["-m", "speedtest", "--json", "--secure", "--source", IP]
    assert seen["kwargs"]["timeout"] == ST.full_timeout_seconds


@pytest.mark.parametrize("runner_result,expected", [
    (_proc(stderr="ERROR: Unable to connect to servers\n", code=1), "Unable to connect"),
    (_proc(stdout="bukan json"), "tidak bisa dibaca"),
    (_proc(stdout=json.dumps({"download": 0, "upload": 0, "ping": 10})), "tidak memberi kecepatan unduh"),
    (subprocess.TimeoutExpired("speedtest", 180), "timeout"),
    (FileNotFoundError("python"), "tidak bisa dijalankan"),
])
def test_run_full_failures(runner_result, expected):
    def runner(cmd, **kwargs):
        if isinstance(runner_result, Exception):
            raise runner_result
        return runner_result

    r = full_mod.run_full(ST, IP, runner=runner)
    assert not r.ok and expected in r.error and r.download_mbps is None


# ------------------------------------------------------------ runner / jadwal

@pytest.fixture
def env(tmp_path):
    cfg = Config(kos="kos-uji", database=tmp_path / "t.db")
    cfg.devices = [DeviceConfig("HP1", operator="Telkomsel", source_ip=IP)]
    conn = db.connect(cfg.database)
    clock = Clock()
    calls = []

    def light(st, ip):
        calls.append(("light", ip))
        return SpeedResult(METHOD_LIGHT, ip, 30.0, 10.0, None, True)

    def full(st, ip):
        calls.append(("full", ip))
        return SpeedResult(METHOD_FULL, ip, 25.0, 20.0, 5.0, True)

    runner = SpeedtestRunner(cfg, conn, now=clock, light=light, full=full, local_ips=lambda: {IP, "10.0.0.5"})
    yield cfg, conn, clock, runner, calls
    conn.close()


def _rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM speedtests ORDER BY id")]


def test_schedule_light_every_15_full_every_60(env):
    cfg, conn, clock, runner, calls = env
    timeline = []
    for minute in range(0, 136):
        clock.t = T0 + timedelta(minutes=minute)
        for r in runner.tick():
            timeline.append((minute, r.method))
    assert timeline == [
        (0, "light"), (15, "full"), (30, "light"), (45, "light"), (60, "light"),
        (75, "full"), (90, "light"), (105, "light"), (120, "light"), (135, "full"),
    ]
    row = _rows(conn)[1]
    assert (row["kos"], row["operator"], row["method"], row["source_ip"], row["ping_ms"], row["download_mbps"],
            row["upload_mbps"], row["ok"], row["error"], row["uploaded"]) == (
        "kos-uji", "Telkomsel", "full", IP, 25.0, 20.0, 5.0, 1, None, 0)
    assert row["ts"] == "2026-11-15T08:15:00+00:00"


def test_schedule_survives_restart(env):
    cfg, conn, clock, runner, calls = env
    runner.tick()
    clock.advance(5)
    again = SpeedtestRunner(cfg, conn, now=clock, light=runner._light, full=runner._full, local_ips=lambda: {IP})
    assert again.tick() == []  # baru 5 menit sejak uji terakhir di database


def test_full_disabled(env):
    cfg, conn, clock, runner, calls = env
    cfg.speedtest.full_interval_minutes = 0
    for minute in range(0, 121):
        clock.t = T0 + timedelta(minutes=minute)
        runner.tick()
    assert {m for m, _ in calls} == {"light"} and len(calls) == 9


def test_speedtest_disabled(env):
    cfg, conn, clock, runner, calls = env
    cfg.speedtest.enabled = False
    assert runner.tick() == [] and calls == []


def test_failure_saved_with_ok_zero(env):
    cfg, conn, clock, runner, calls = env
    runner._light = lambda st, ip: SpeedResult(METHOD_LIGHT, ip, None, None, None, False, "unduh gagal: putus")
    runner.tick()
    row = _rows(conn)[0]
    assert (row["ok"], row["error"], row["download_mbps"]) == (0, "unduh gagal: putus", None)
    clock.advance(15)
    assert len(runner.tick()) == 1  # gagal tetap dijadwalkan ulang seperti biasa


def test_unexpected_exception_becomes_failed_row(env):
    cfg, conn, clock, runner, calls = env

    def boom(st, ip):
        raise RuntimeError("meledak")

    runner._light = boom
    result = runner.tick()[0]
    assert not result.ok and "meledak" in result.error
    assert _rows(conn)[0]["ok"] == 0


def test_source_ip_missing_from_laptop(env):
    cfg, conn, clock, runner, calls = env
    runner._local_ips = lambda: {"10.0.0.5"}
    result = runner.tick()[0]
    assert not result.ok and "tidak ada di laptop" in result.error and calls == []
    assert _rows(conn)[0]["ok"] == 0


def test_device_without_source_ip_is_skipped_with_one_warning(env, caplog):
    cfg, conn, clock, runner, calls = env
    cfg.devices.append(DeviceConfig("HP2", operator="Tri"))
    with caplog.at_level("WARNING"):
        runner.tick()
        clock.advance(15)
        runner.tick()
    assert [c[1] for c in calls] == [IP, IP]
    assert sum("source_ip belum diisi" in r.message for r in caplog.records) == 1


def test_two_operators_and_operator_from_measurements(env):
    cfg, conn, clock, runner, calls = env
    cfg.devices.append(DeviceConfig("HP2", source_ip="10.0.0.5"))  # operator tidak diisi di config
    assert [t.operator for t in runner.targets()] == ["Telkomsel"]
    db.insert_measurement(conn, {"ts": "t", "kos": "kos-uji", "device_serial": "HP2", "operator": "Tri", "raw_ok": 1})
    runner.tick()
    assert {(r["operator"], r["source_ip"]) for r in _rows(conn)} == {("Telkomsel", IP), ("Tri", "10.0.0.5")}


# ------------------------------------------------------------ config, interfaces, CLI

def test_config_speedtest_and_source_ip(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "speedtest:\n  light_interval_minutes: 5\n  full_interval_minutes: 0\n  ping_host: 8.8.8.8\n"
        "devices:\n  - serial: A\n    operator: XL\n    source_ip: 192.168.42.10\n  - serial: B\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert (cfg.speedtest.light_interval_minutes, cfg.speedtest.full_interval_minutes, cfg.speedtest.ping_host) == (
        5, 0, "8.8.8.8")
    assert cfg.speedtest.ping_count == 3 and cfg.speedtest.enabled is True
    assert (cfg.device("A").source_ip, cfg.device("B").source_ip) == ("192.168.42.10", None)


def test_interface_hints():
    assert "USB tethering Android" in netif.Interface("Ethernet 3", "192.168.42.129").hint
    assert netif.Interface("usb0", "192.168.238.5").hint == "kemungkinan USB tethering"
    assert netif.Interface("Wi-Fi", "192.168.1.7").hint == "Wi-Fi"
    assert netif.Interface("lo", "127.0.0.1").hint == "loopback"
    assert netif.Interface("eth0", "10.1.2.3").hint == ""


def test_list_interfaces_real_psutil():
    interfaces = netif.list_interfaces()
    assert any(i.ipv4.startswith("127.") for i in interfaces)
    assert netif.local_ipv4() == {i.ipv4 for i in interfaces}


def test_cli_interfaces(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CEKSINYAL_CONFIG", raising=False)
    (tmp_path / "config.yaml").write_text(
        f"devices:\n  - serial: A\n    operator: XL\n    source_ip: {IP}\n"
        "  - serial: B\n    operator: Tri\n    source_ip: 10.9.9.9\n", encoding="utf-8")
    monkeypatch.setattr(netif, "list_interfaces", lambda: [
        netif.Interface("Ethernet 3", IP, "255.255.255.0", True), netif.Interface("Wi-Fi", "192.168.1.7", None, True)])
    assert main(["interfaces"]) == 0
    out = capsys.readouterr().out
    assert "Ethernet 3" in out and "dipakai di config: XL" in out
    assert "TIDAK ada di laptop saat ini: 10.9.9.9 (Tri)" in out


def test_cli_speedtest(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CEKSINYAL_CONFIG", raising=False)
    assert main(["speedtest"]) == 1  # tidak ada source_ip
    assert "source_ip" in capsys.readouterr().out
    (tmp_path / "config.yaml").write_text(
        f"kos: k\ndatabase: s.db\ndevices:\n  - serial: A\n    operator: XL\n    source_ip: {IP}\n", encoding="utf-8")
    monkeypatch.setattr(netif, "local_ipv4", lambda: {IP})
    from ceksinyal.speedtest import runner as runner_mod
    monkeypatch.setattr(runner_mod, "run_light", lambda st, ip: SpeedResult(METHOD_LIGHT, ip, 30.0, 10.0, None, True))
    monkeypatch.setattr(runner_mod, "run_full", lambda st, ip: SpeedResult(METHOD_FULL, ip, error="gagal"))
    monkeypatch.setattr(SpeedtestRunner.__init__, "__defaults__",
                        (db.utc_now, runner_mod.run_light, runner_mod.run_full, netif.local_ipv4))
    assert main(["speedtest", "--no-save"]) == 0
    assert "10.0 Mbps" in capsys.readouterr().out
    assert main(["speedtest", "--full"]) == 2
    assert "GAGAL" in capsys.readouterr().out
    conn = db.connect(tmp_path / "s.db")
    assert [tuple(r) for r in conn.execute("SELECT method, ok FROM speedtests")] == [("full", 0)]
    conn.close()
