"""HP dual-SIM yang dibaca dua slot sekaligus: satu serial ditulis dua kali di devices (phone_id 0 dan 1)."""

import logging
from pathlib import Path

import pytest

from ceksinyal import db, selftest
from ceksinyal.collector.service import Collector, RawStore, probe
from ceksinyal.config import ConfigError, load_config, sim_key, split_sim_key
from ceksinyal.fix_sinr import plan
from ceksinyal.speedtest import METHOD_LIGHT, SpeedResult
from ceksinyal.speedtest.runner import SpeedtestRunner
from ceksinyal.uploader.sheets import aggregate_minutes

from tests.test_collector import Clock, FakeAdb
from tests.test_run_selftest import FakeAdb as SelftestAdb

FIXTURES = Path(__file__).parent / "fixtures"
S22 = (FIXTURES / "real_samsung_s22_android15_dualsim.txt").read_text(encoding="utf-8", errors="replace")
IP = "10.20.249.202"

DUAL = f"""
kos: kos-uji
database: t.db
raw_dir: raw
devices:
  - serial: S22
    operator: Telkomsel
    phone_id: 0
    source_ip: {IP}
  - serial: S22
    operator: Indosat
    phone_id: 1
"""


def _load(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return load_config(p)


@pytest.fixture
def cfg(tmp_path):
    return _load(tmp_path, DUAL)


# ---------------------------------------------------------------- config

def test_config_two_slots_get_sim_keys(cfg):
    a, b = cfg.devices
    assert (a.multi_sim, b.multi_sim) == (True, True)
    assert (a.key, b.key) == ("S22/sim1", "S22/sim2")
    assert (a.label, b.label) == ("S22 SIM 1", "S22 SIM 2")
    assert cfg.device_serials() == ["S22"] and len(cfg.device_entries("S22")) == 2


def test_single_entry_keeps_plain_serial(tmp_path):
    # Satu slot saja (seperti sebelumnya): device_serial tetap serial biasa, data lama tetap satu seri.
    cfg = _load(tmp_path, "devices:\n  - serial: S22\n    operator: Telkomsel\n    phone_id: 1\n")
    assert (cfg.devices[0].multi_sim, cfg.devices[0].key, cfg.devices[0].label) == (False, "S22", "S22")


def test_sim_key_roundtrip():
    assert sim_key("S22", 0) == "S22/sim1" and sim_key("S22", 1) == "S22/sim2"
    assert split_sim_key("S22/sim2") == ("S22", 2)
    assert split_sim_key("192.168.1.5:5555") == ("192.168.1.5:5555", None)  # serial ADB Wi-Fi
    assert split_sim_key("S22/simX") == ("S22/simX", None)


@pytest.mark.parametrize("entries,message", [
    ("  - serial: A\n    phone_id: 0\n  - serial: A\n", "wajib punya phone_id"),
    ("  - serial: A\n    phone_id: 0\n  - serial: A\n    phone_id: 0\n", "phone_id ganda"),
    ("  - serial: A\n    phone_id: 0\n    source_ip: 1.1.1.1\n  - serial: A\n    phone_id: 1\n    source_ip: 2.2.2.2\n",
     "hanya boleh diisi di satu slot"),
    ("  - serial: A\n    source_ip: 1.1.1.1\n  - serial: B\n    source_ip: 1.1.1.1\n", "dipakai dua entri"),
])
def test_config_invalid_dual_sim(tmp_path, entries, message):
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, "devices:\n" + entries)


# ---------------------------------------------------------------- collector

def test_collector_one_dumpsys_two_rows(cfg):
    conn = db.connect(cfg.database)
    adb = FakeAdb()
    adb.connect("S22", S22)
    adb.sdk["S22"] = "35"
    try:
        assert Collector(cfg, conn, adb, now=Clock()).tick() == 2
        rows = {r["device_serial"]: dict(r) for r in conn.execute("SELECT * FROM measurements")}
    finally:
        conn.close()
    assert adb.dumps == ["S22"]  # satu perintah adb untuk dua slot
    assert set(rows) == {"S22/sim1", "S22/sim2"}
    sim1, sim2 = rows["S22/sim1"], rows["S22/sim2"]
    assert (sim1["operator"], sim1["rsrp"], sim1["pci"], sim1["earfcn"]) == ("Telkomsel", -76, 474, 1850)
    assert (sim2["operator"], sim2["rsrp"], sim2["pci"], sim2["earfcn"]) == ("Indosat", -83, 115, 1475)
    assert sim1["cell_id"] is None and sim2["cell_id"] is None  # Samsung menyamarkan sebagian mCi


def test_collector_disconnect_event_per_operator(cfg):
    conn = db.connect(cfg.database)
    adb = FakeAdb()
    adb.connect("S22", S22)
    clock = Clock()
    try:
        collector = Collector(cfg, conn, adb, now=clock)
        collector.tick()
        adb.disconnect("S22")
        clock.advance(10)
        collector.tick()
        clock.advance(10)
        collector.tick()  # masih putus: tidak dicatat ulang
        events = [dict(r) for r in conn.execute("SELECT operator, detail FROM events")]
    finally:
        conn.close()
    assert sorted(e["operator"] for e in events) == ["Indosat", "Telkomsel"]
    assert all("terputus" in e["detail"] for e in events)


def test_probe_reports_each_slot(cfg, tmp_path):
    adb = FakeAdb()
    adb.connect("S22", S22)
    results = probe(cfg, adb, RawStore(tmp_path / "raw"))
    assert [(r.label, r.operator, r.reading.rsrp) for r in results] == [
        ("S22 SIM 1", "Telkomsel", -76), ("S22 SIM 2", "Indosat", -83)]
    assert results[0].raw_path == results[1].raw_path and results[0].raw_path.exists()
    only = probe(cfg, adb, RawStore(tmp_path / "raw"), phone_id=1)
    assert [(r.operator, r.reading.phone_id) for r in only] == [("Indosat", 1)]


# ---------------------------------------------------------------- speedtest & selftest

def test_speedtest_only_data_sim(cfg, caplog):
    conn = db.connect(cfg.database)
    calls = []

    def light(st, ip):
        calls.append(ip)
        return SpeedResult(METHOD_LIGHT, ip, 30.0, 10.0, None, True)

    try:
        runner = SpeedtestRunner(cfg, conn, now=Clock(), light=light, local_ips=lambda: {IP})
        with caplog.at_level(logging.WARNING):
            targets = runner.targets()
            runner.targets()
        assert [(t.serial, t.operator) for t in targets] == [("S22/sim1", "Telkomsel")]
        assert not any("source_ip belum diisi" in r.message for r in caplog.records)
        assert sum("bukan SIM data" in r.message for r in caplog.records) == 1
    finally:
        conn.close()


def test_selftest_checks_each_slot(cfg):
    checks = {c.name: c for c in selftest.run_checks(cfg, SelftestAdb({"S22": ("device", S22)}),
                                                      local_ips=lambda: {IP})}
    assert checks["HP S22 SIM 1"].detail.startswith("Telkomsel: LTE, RSRP -76")
    assert checks["HP S22 SIM 2"].detail.startswith("Indosat: LTE, RSRP -83")
    assert checks["tethering S22 SIM 1"].status == "OK"
    assert checks["tethering S22 SIM 2"].status == "LEWATI" and "bukan SIM data" in checks["tethering S22 SIM 2"].detail
    slots = [c for name, c in checks.items() if "S22" in name]
    assert len(slots) == 4 and all(c.status != "GAGAL" for c in slots)


# ---------------------------------------------------------------- data hilir

def test_uploader_minutes_keep_slots_apart():
    rows = [{"id": i, "ts": f"2026-11-18T05:00:{s:02d}+00:00", "kos": "k", "device_serial": key, "operator": op, "rat": "LTE",
             "rsrp": rsrp, "rsrq": -10, "sinr": 5.0, "pci": None, "cell_id": None, "earfcn": 1850, "band": 3,
             "raw_ok": 1}
            for i, (s, (key, op, rsrp)) in enumerate(
                (s, slot) for s in (0, 10) for slot in (("S22/sim1", "Telkomsel", -76), ("S22/sim2", "Indosat", -83)))]
    minutes = aggregate_minutes(rows)
    assert len(minutes) == 2


def test_fix_sinr_serial_matches_slots(tmp_path):
    conn = db.connect(tmp_path / "f.db")
    for key in ("S22/sim1", "S22/sim2", "S2X", "LAIN"):
        db.insert_measurement(conn, {"ts": "t", "kos": "k", "device_serial": key, "operator": "X", "rat": "LTE",
                                     "sinr": 45.0, "raw_ok": 1})
    try:
        assert set(plan(conn, serial="S22").serials) == {"S22/sim1", "S22/sim2"}
        assert set(plan(conn, serial="S2_").serials) == set()  # '_' bukan wildcard
    finally:
        conn.close()


def test_dashboard_labels_group_slots_of_one_phone():
    pd = pytest.importorskip("pandas")
    from ceksinyal.dashboard import data

    ms = pd.DataFrame({"device": ["S22/sim1", "S22/sim2", "HPLAIN"], "kos": ["k", "k", "k"],
                       "operator": ["Telkomsel", "Indosat", "XL"]})
    labels = data.device_label_map(ms)
    assert labels["S22/sim1"] == "HP 1 · SIM 1" and labels["S22/sim2"] == "HP 1 · SIM 2"
    assert labels["S22"] == "HP 1" and labels["HPLAIN"] == "HP 2"
