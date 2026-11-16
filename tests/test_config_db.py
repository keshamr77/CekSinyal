import os
import sqlite3
from pathlib import Path

import pytest

from ceksinyal import db
from ceksinyal.collector.parser import parse_registry
from ceksinyal.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_loads():
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg.kos == "kos-a"
    assert cfg.collector.interval_seconds == 10
    assert cfg.collector.max_devices == 2
    assert cfg.database == ROOT / "data" / "ceksinyal.db"
    assert cfg.device("R58M12345AB").operator == "Telkomsel"
    assert cfg.device("tidak-ada") is None


def test_missing_explicit_config_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "tidak-ada.yaml")


def test_missing_default_config_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CEKSINYAL_CONFIG", raising=False)
    cfg = load_config()
    assert cfg.devices == [] and cfg.collector.interval_seconds == 10


def test_invalid_values_raise(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("collector:\n  interval_seconds: 0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)
    p.write_text("devices:\n  - operator: XL\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_dotenv_loaded_without_overriding(tmp_path, monkeypatch):
    (tmp_path / "c.yaml").write_text("kos: x\n", encoding="utf-8")
    (tmp_path / ".env").write_text("# komentar\nCEKSINYAL_T1='abc'\nCEKSINYAL_T2=baru\n", encoding="utf-8")
    monkeypatch.delenv("CEKSINYAL_T1", raising=False)
    monkeypatch.setenv("CEKSINYAL_T2", "lama")
    load_config(tmp_path / "c.yaml")
    assert os.environ["CEKSINYAL_T1"] == "abc"
    assert os.environ["CEKSINYAL_T2"] == "lama"
    monkeypatch.delenv("CEKSINYAL_T1")


def test_db_schema_and_inserts(tmp_path):
    conn = db.connect(tmp_path / "sub" / "x.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"measurements", "speedtests", "diagnoses", "events"} <= tables
    db.insert_measurement(conn, {"ts": "t", "kos": "k", "device_serial": "s", "rat": "NONE", "raw_ok": 1})
    db.insert_event(conn, "t", "k", None, db.EVENT_COLLECTOR_ERROR, "d")
    m = dict(conn.execute("SELECT * FROM measurements").fetchone())
    assert m["rsrp"] is None and m["uploaded"] == 0
    assert conn.execute("SELECT notified FROM events").fetchone()[0] == 0
    conn.close()


OLD_MEASUREMENTS = """
CREATE TABLE measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kos TEXT NOT NULL,
    device_serial TEXT NOT NULL, operator TEXT, rat TEXT, rsrp INTEGER, rsrq INTEGER, sinr REAL,
    rssi INTEGER, pci INTEGER, cell_id INTEGER, earfcn INTEGER, band INTEGER,
    raw_ok INTEGER NOT NULL DEFAULT 1, uploaded INTEGER NOT NULL DEFAULT 0
);
"""


def test_migration_adds_columns_without_losing_rows(tmp_path):
    path = tmp_path / "lama.db"
    old = sqlite3.connect(path)
    old.executescript(OLD_MEASUREMENTS)
    old.executemany(
        "INSERT INTO measurements (ts, kos, device_serial, rat, rsrp, sinr, uploaded) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("t1", "k", "HP1", "LTE", -101, 25.0, 1), ("t2", "k", "HP1", "LTE", -99, 4.5, 0)],
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(measurements)")}
    assert {"sinr_raw", "sinr_unit"} <= cols
    rows = [dict(r) for r in conn.execute("SELECT * FROM measurements ORDER BY id")]
    assert [(r["ts"], r["rsrp"], r["sinr"], r["uploaded"], r["sinr_raw"]) for r in rows] == [
        ("t1", -101, 25.0, 1, None), ("t2", -99, 4.5, 0, None)]
    assert db.migrate(conn) == []  # kedua kali tidak ada yang berubah
    db.insert_measurement(conn, {"ts": "t3", "kos": "k", "device_serial": "HP1", "sinr": 2.5,
                                 "sinr_raw": 25, "sinr_unit": "deci_db", "raw_ok": 1})
    assert conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 3
    conn.close()


def test_new_db_has_sinr_columns(tmp_path):
    conn = db.connect(tmp_path / "baru.db")
    assert db.migrate(conn) == []
    cols = [r[1] for r in conn.execute("PRAGMA table_info(measurements)")]
    assert "sinr_raw" in cols and "sinr_unit" in cols
    conn.close()


def test_reading_raw_sinr_is_stored(tmp_path):
    text = (
        "    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE)}\n"
        "    mSignalStrength=SignalStrength:{mLte=CellSignalStrengthLte: rssi=-70 rsrp=-101 "
        "rsrq=-11 rssnr=25 level=3,mNr=Invalid}\n"
    )
    reading = parse_registry(text, lte_sinr_unit="deci_db")
    conn = db.connect(tmp_path / "x.db")
    db.insert_measurement(conn, {"ts": "t", "kos": "k", "device_serial": "s", "raw_ok": 1,
                                 **reading.measurement_fields()})
    row = dict(conn.execute("SELECT sinr, sinr_raw, sinr_unit FROM measurements").fetchone())
    assert row == {"sinr": 2.5, "sinr_raw": 25, "sinr_unit": "deci_db"}
    conn.close()


def test_device_sinr_unit_config(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("devices:\n  - serial: A\n  - serial: B\n    sinr_unit: DECI_DB\n", encoding="utf-8")
    cfg = load_config(p)
    assert (cfg.device("A").sinr_unit, cfg.device("B").sinr_unit) == ("auto", "deci_db")
    p.write_text("devices:\n  - serial: A\n    sinr_unit: desibel\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_connect_from_many_threads_at_once(tmp_path):
    """`run` membuka satu koneksi per thread ke database yang mungkin baru dibuat: tidak boleh 'database is locked'."""
    import threading

    path = tmp_path / "ramai.db"
    errors = []
    start = threading.Barrier(12)

    def worker(i):
        try:
            start.wait()
            conn = db.connect(path)
            db.insert_event(conn, "t", "k", None, db.EVENT_COLLECTOR_ERROR, str(i))
            conn.close()
        except Exception as exc:  # dikumpulkan supaya terlihat di assert
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 12
    conn.close()
