from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ceksinyal import db
from ceksinyal.collector.adb import AdbError, AdbNotFoundError, DeviceInfo
from ceksinyal.collector.service import (
    SDK_PROP, Collector, RawStore, lte_sinr_unit, pick_operator_alpha, probe,
)
from ceksinyal.config import Config, DeviceConfig

FIXTURES = Path(__file__).parent / "fixtures"
LTE_TEXT = (FIXTURES / "synthetic_lte_android10.txt").read_text(encoding="utf-8")
UNKNOWN_TEXT = (FIXTURES / "synthetic_unknown_format.txt").read_text(encoding="utf-8")


class FakeAdb:
    def __init__(self):
        self.listing: list[DeviceInfo] | Exception = []
        self.outputs: dict[str, str | Exception] = {}
        self.props: dict[str, str] = {}
        self.sdk: dict[str, str] = {}
        self.sdk_calls: list[str] = []
        self.dumps: list[str] = []

    def connect(self, serial, text=LTE_TEXT, state="device"):
        if isinstance(self.listing, Exception):
            self.listing = []
        self.listing = [d for d in self.listing if d.serial != serial] + [DeviceInfo(serial, state)]
        self.outputs[serial] = text

    def disconnect(self, serial):
        self.listing = [d for d in self.listing if d.serial != serial]

    def devices(self):
        if isinstance(self.listing, Exception):
            raise self.listing
        return list(self.listing)

    def dumpsys_registry(self, serial):
        self.dumps.append(serial)
        out = self.outputs[serial]
        if isinstance(out, Exception):
            raise out
        return out

    def getprop(self, serial, key):
        if key == SDK_PROP:
            self.sdk_calls.append(serial)
            if serial not in self.sdk:
                raise AdbError("getprop gagal")
            return self.sdk[serial]
        if serial not in self.props:
            raise AdbError("getprop gagal")
        return self.props[serial]


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 14, 8, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


@pytest.fixture
def env(tmp_path):
    cfg = Config(kos="kos-uji", database=tmp_path / "t.db", raw_dir=tmp_path / "raw")
    cfg.devices = [DeviceConfig("HP1", operator="Telkomsel"), DeviceConfig("HP2", operator="Indosat")]
    conn = db.connect(cfg.database)
    adb = FakeAdb()
    clock = Clock()
    collector = Collector(cfg, conn, adb, now=clock)
    yield cfg, conn, adb, clock, collector
    conn.close()


def _rows(conn, table):
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]


def test_tick_saves_measurements_for_two_devices(env):
    cfg, conn, adb, clock, collector = env
    adb.connect("HP1")
    adb.connect("HP2")
    assert collector.tick() == 2
    rows = _rows(conn, "measurements")
    assert {r["operator"] for r in rows} == {"Telkomsel", "Indosat"}
    r = rows[0]
    assert (r["kos"], r["rat"], r["rsrp"], r["sinr"], r["band"], r["raw_ok"], r["uploaded"]) == (
        "kos-uji", "LTE", -98, 9.0, 3, 1, 0)
    assert r["ts"] == "2026-11-14T08:00:00+00:00"


def test_disconnect_recorded_once_and_reconnect(env):
    cfg, conn, adb, clock, collector = env
    adb.connect("HP1")
    adb.connect("HP2")
    collector.tick()
    adb.disconnect("HP2")
    for _ in range(3):
        clock.advance(10)
        assert collector.tick() == 1
    events = _rows(conn, "events")
    assert len(events) == 1
    assert events[0]["type"] == "collector_error" and events[0]["operator"] == "Indosat"
    assert "HP2 terputus" in events[0]["detail"]

    adb.connect("HP2")
    clock.advance(10)
    assert collector.tick() == 2
    adb.disconnect("HP2")
    clock.advance(10)
    collector.tick()
    assert len(_rows(conn, "events")) == 2


def test_configured_device_never_connected_logs_event(env):
    cfg, conn, adb, clock, collector = env
    adb.connect("HP1")
    collector.tick()
    collector.tick()
    events = _rows(conn, "events")
    assert len(events) == 1 and "HP2 belum terhubung" in events[0]["detail"]


def test_unauthorized_device_is_not_read(env):
    cfg, conn, adb, clock, collector = env
    adb.connect("HP1", state="unauthorized")
    collector.tick()
    assert adb.dumps == []
    assert any("unauthorized" in e["detail"] for e in _rows(conn, "events"))


def test_read_error_mid_session_does_not_crash(env):
    cfg, conn, adb, clock, collector = env
    adb.connect("HP1")
    collector.tick()
    adb.outputs["HP1"] = AdbError("error: device 'HP1' not found")
    clock.advance(10)
    collector.tick()
    assert any("HP1 terputus" in e["detail"] for e in _rows(conn, "events"))


def test_parse_failure_saves_raw_once_per_hour(env):
    cfg, conn, adb, clock, collector = env
    cfg.devices = cfg.devices[:1]
    adb.connect("HP1", text=UNKNOWN_TEXT)
    for _ in range(5):
        collector.tick()
        clock.advance(10)
    files = sorted(cfg.raw_dir.glob("HP1_*.txt"))
    assert len(files) == 1 and files[0].read_text(encoding="utf-8") == UNKNOWN_TEXT
    rows = _rows(conn, "measurements")
    assert len(rows) == 5 and all(r["raw_ok"] == 0 and r["rat"] is None for r in rows)
    events = _rows(conn, "events")
    assert len(events) == 1 and "parsing gagal" in events[0]["detail"]

    clock.advance(3600)
    collector.tick()
    assert len(list(cfg.raw_dir.glob("HP1_*.txt"))) == 2


def test_raw_rate_limit_survives_restart(tmp_path):
    t0 = datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)
    RawStore(tmp_path, 3600).maybe_save("192.168.1.20:5555", "a", t0)
    fresh = RawStore(tmp_path, 3600)
    assert fresh.maybe_save("192.168.1.20:5555", "b", t0 + timedelta(minutes=30)) is None
    assert fresh.maybe_save("192.168.1.20:5555", "c", t0 + timedelta(minutes=61)) is not None
    assert all(":" not in p.name for p in tmp_path.iterdir())


def test_adb_missing_is_reported_once(env):
    cfg, conn, adb, clock, collector = env
    adb.listing = AdbNotFoundError("adb tidak ditemukan")
    assert collector.tick() == 0
    assert collector.tick() == 0
    assert len(_rows(conn, "events")) == 1
    adb.connect("HP1")
    adb.connect("HP2")
    assert collector.tick() == 2


def test_auto_mode_limits_devices_and_uses_getprop(env):
    cfg, conn, adb, clock, collector = env
    cfg.devices = []
    for s in ("A", "B", "C"):
        adb.connect(s)
    adb.props = {"A": "TELKOMSEL,XL", "B": ""}
    assert collector.tick() == 2
    ops = {r["device_serial"]: r["operator"] for r in _rows(conn, "measurements")}
    assert ops == {"A": "TELKOMSEL", "B": "IM3"}  # B: getprop kosong -> nama dari dumpsys

    adb.disconnect("A")
    clock.advance(10)
    collector.tick()
    assert any("HP A terputus" in e["detail"] for e in _rows(conn, "events"))


def test_unexpected_exception_in_tick_does_not_stop_run(env, monkeypatch):
    cfg, conn, adb, clock, collector = env
    adb.connect("HP1")
    adb.connect("HP2")
    calls = {"n": 0}
    real_tick = collector.tick

    def flaky_tick():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_tick()

    monkeypatch.setattr(collector, "tick", flaky_tick)
    collector._sleep = lambda s: None
    collector.run(iterations=3)
    assert calls["n"] == 3
    assert len(_rows(conn, "measurements")) == 4
    assert any("boom" in e["detail"] for e in _rows(conn, "events"))


def test_run_uses_thread_pool(env):
    cfg, conn, adb, clock, collector = env
    adb.connect("HP1")
    adb.connect("HP2")
    collector._sleep = lambda s: None
    collector.run(iterations=2)
    assert len(_rows(conn, "measurements")) == 4


def test_probe_saves_raw_with_prefix(env):
    cfg, conn, adb, clock, collector = env
    adb.connect("HP1")
    adb.connect("HP9", text=UNKNOWN_TEXT, state="device")
    results = {r.serial: r for r in probe(cfg, adb, RawStore(cfg.raw_dir))}
    assert results["HP1"].reading.ok and results["HP1"].operator == "Telkomsel"
    assert results["HP1"].raw_path.name.startswith("probe_HP1_")
    assert not results["HP9"].reading.ok
    # file probe tidak menghalangi dump collector
    assert RawStore(cfg.raw_dir, 3600).maybe_save("HP1", "x", db.utc_now()) is not None


@pytest.mark.parametrize("prop,phone_id,expected", [
    ("Telkomsel", None, "Telkomsel"),
    ("Telkomsel,XL", 1, "XL"),
    (",XL", 0, "XL"),
    ("", 0, None),
    (None, None, None),
])
def test_pick_operator_alpha(prop, phone_id, expected):
    assert pick_operator_alpha(prop, phone_id) == expected


def _lte_rssnr(rssnr: int) -> str:
    return (
        "  Phone Id=0\n"
        "    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE)}\n"
        "    mSignalStrength=SignalStrength:{mLte=CellSignalStrengthLte: rssi=-70 rsrp=-101 "
        f"rsrq=-11 rssnr={rssnr} cqi=0 ta=0 level=3,mNr=Invalid}}\n"
    )


def _sinr_rows(conn):
    return [r["sinr"] for r in _rows(conn, "measurements")]


def _feed(collector, adb, clock, serial, values):
    for v in values:
        adb.outputs[serial] = _lte_rssnr(v)
        collector.tick()
        clock.advance(10)


@pytest.mark.parametrize("configured,sdk,unit", [
    ("auto", 29, "deci_db"), ("auto", 28, "deci_db"), ("auto", 30, "db"), ("auto", 34, "db"),
    ("auto", None, None), ("db", 29, "db"), ("deci_db", 34, "deci_db"),
])
def test_lte_sinr_unit(configured, sdk, unit):
    assert lte_sinr_unit(configured, sdk) == unit


def test_android10_device_uses_deci_db(env):
    cfg, conn, adb, clock, collector = env
    cfg.devices = cfg.devices[:1]
    adb.connect("HP1")
    adb.sdk["HP1"] = "29"
    _feed(collector, adb, clock, "HP1", [25, -15, 55, 300, 2147483647])
    assert _sinr_rows(conn) == [2.5, -1.5, 5.5, 30.0, None]
    assert adb.sdk_calls == ["HP1"]  # SDK dibaca sekali per HP
    assert _rows(conn, "events") == []


def test_android11_device_uses_db(env):
    cfg, conn, adb, clock, collector = env
    cfg.devices = cfg.devices[:1]
    adb.connect("HP1")
    adb.sdk["HP1"] = "30\n"
    _feed(collector, adb, clock, "HP1", [5, 25])
    assert _sinr_rows(conn) == [5.0, 25.0]


def test_db_device_with_out_of_range_rssnr_switches_to_deci_db(env):
    cfg, conn, adb, clock, collector = env
    cfg.devices = cfg.devices[:1]
    adb.connect("HP1")
    adb.sdk["HP1"] = "33"
    _feed(collector, adb, clock, "HP1", [5, 55, 25, 70])
    # Sampel pemicu langsung dihitung ulang; setelahnya permanen 0,1 dB.
    assert _sinr_rows(conn) == [5.0, 5.5, 2.5, 7.0]
    events = _rows(conn, "events")
    assert len(events) == 1
    assert events[0]["type"] == "collector_error" and events[0]["operator"] == "Telkomsel"
    assert "rssnr=55" in events[0]["detail"] and "0,1 dB" in events[0]["detail"]


def test_unknown_sdk_uses_db_with_guard_and_retries_getprop(env):
    cfg, conn, adb, clock, collector = env
    cfg.devices = cfg.devices[:1]
    adb.connect("HP1")
    _feed(collector, adb, clock, "HP1", [12, 45, 12])
    assert _sinr_rows(conn) == [12.0, 4.5, 1.2]
    assert len(adb.sdk_calls) == 3  # getprop gagal -> dicoba lagi tiap putaran
    adb.sdk["HP1"] = "29"
    _feed(collector, adb, clock, "HP1", [12, 12])
    assert len(adb.sdk_calls) == 4


def test_garbage_rssnr_does_not_trigger_guard(env):
    cfg, conn, adb, clock, collector = env
    cfg.devices = cfg.devices[:1]
    adb.connect("HP1")
    adb.sdk["HP1"] = "33"
    _feed(collector, adb, clock, "HP1", [999, 25])
    assert _sinr_rows(conn) == [None, 25.0]
    assert _rows(conn, "events") == []


def test_config_unit_override(env):
    cfg, conn, adb, clock, collector = env
    cfg.devices = [DeviceConfig("HP1", operator="Telkomsel", sinr_unit="deci_db"),
                   DeviceConfig("HP2", operator="Indosat", sinr_unit="db")]
    adb.connect("HP1", text=_lte_rssnr(25))
    adb.connect("HP2", text=_lte_rssnr(55))
    adb.sdk = {"HP1": "33", "HP2": "29"}
    collector.tick()
    rows = {r["device_serial"]: r["sinr"] for r in _rows(conn, "measurements")}
    assert rows == {"HP1": 2.5, "HP2": None}  # override tidak ditimpa pengaman
    assert adb.sdk_calls == [] and _rows(conn, "events") == []


def test_probe_uses_sdk(env):
    cfg, conn, adb, clock, collector = env
    adb.connect("HP1", text=_lte_rssnr(25))
    adb.sdk["HP1"] = "29"
    (res,) = probe(cfg, adb, RawStore(cfg.raw_dir))
    assert (res.reading.sinr, res.reading.sinr_unit) == (2.5, "deci_db")
