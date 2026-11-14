import os
from pathlib import Path

import pytest

from ceksinyal import db
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
