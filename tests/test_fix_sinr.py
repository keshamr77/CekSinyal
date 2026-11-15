import sqlite3

import pytest

from ceksinyal import db
from ceksinyal.__main__ import main
from ceksinyal.fix_sinr import fix_sinr, is_misscaled

OLD_MEASUREMENTS = """
CREATE TABLE measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kos TEXT NOT NULL,
    device_serial TEXT NOT NULL, operator TEXT, rat TEXT, rsrp INTEGER, rsrq INTEGER, sinr REAL,
    rssi INTEGER, pci INTEGER, cell_id INTEGER, earfcn INTEGER, band INTEGER,
    raw_ok INTEGER NOT NULL DEFAULT 1, uploaded INTEGER NOT NULL DEFAULT 0
);
"""


def _old_db(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_MEASUREMENTS)
    conn.executemany(
        "INSERT INTO measurements (ts, kos, device_serial, rat, sinr) VALUES ('t', 'k', ?, ?, ?)", rows
    )
    conn.commit()
    conn.close()
    return path


def _sinr(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT sinr FROM measurements ORDER BY id")]
    finally:
        conn.close()


@pytest.mark.parametrize("value,expected", [
    (25.0, True), (-15.0, True), (10.0, True), (30.0, True), (-8.0, True), (0.0, True), (2.0, True),
    (5.0, False), (7.0, False), (-3.0, False), (4.5, False), (11.0, False), (-25.0, False),
    (24.0, False), (None, False),
])
def test_is_misscaled(value, expected):
    assert is_misscaled(value) is expected


def test_fix_example_rows(tmp_path):
    path = _old_db(tmp_path / "lama.db", [("HP1", "LTE", v) for v in (25.0, -15.0, 4.5, 5.0)])
    result = fix_sinr(path)
    assert _sinr(path) == [2.5, -1.5, 4.5, 5.0]
    assert (result.checked, result.corrected, result.written) == (4, 2, True)
    s = result.serials["HP1"]
    assert (s.median_before, s.median_after) == (4.75, 3.5)
    # backup berisi data asli
    assert result.backup.is_file() and result.backup.suffix == ".db"
    assert _sinr(result.backup) == [25.0, -15.0, 4.5, 5.0]
    # baris yang dikoreksi sekarang membawa nilai mentah, jadi tidak dikoreksi dua kali
    conn = db.connect(path)
    rows = [tuple(r) for r in conn.execute("SELECT sinr, sinr_raw, sinr_unit FROM measurements ORDER BY id")]
    conn.close()
    assert rows == [(2.5, 25, "deci_db"), (-1.5, -15, "deci_db"), (4.5, None, None), (5.0, None, None)]
    again = fix_sinr(path)
    assert again.corrected == 0 and _sinr(path) == [2.5, -1.5, 4.5, 5.0]


def test_dry_run_writes_nothing(tmp_path):
    path = _old_db(tmp_path / "lama.db", [("HP1", "LTE", 25.0)])
    result = fix_sinr(path, dry_run=True)
    assert result.corrected == 1 and not result.written and result.backup is None
    assert _sinr(path) == [25.0]
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(measurements)")}
    conn.close()
    assert "sinr_raw" not in cols  # bahkan migrasi pun tidak dijalankan
    assert list(tmp_path.glob("*.backup-*")) == []


def test_serial_filter_and_non_lte_untouched(tmp_path):
    path = _old_db(tmp_path / "lama.db", [
        ("HP1", "LTE", 25.0), ("HP2", "LTE", 25.0), ("HP1", "NR", 20.0), ("HP1", "NONE", None),
    ])
    fix_sinr(path, serial="HP1")
    assert _sinr(path) == [2.5, 25.0, 20.0, None]


def test_new_rows_with_raw_value_untouched(tmp_path):
    conn = db.connect(tmp_path / "baru.db")
    db.insert_measurement(conn, {"ts": "t", "kos": "k", "device_serial": "HP1", "rat": "LTE",
                                 "sinr": 25.0, "sinr_raw": 25, "sinr_unit": "db", "raw_ok": 1})
    conn.close()
    result = fix_sinr(tmp_path / "baru.db")
    assert result.checked == 0 and _sinr(tmp_path / "baru.db") == [25.0]


def test_missing_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        fix_sinr(tmp_path / "tidak-ada.db")


def test_cli(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CEKSINYAL_CONFIG", raising=False)
    path = _old_db(tmp_path / "lama.db", [("HP1", "LTE", 25.0), ("HP1", "LTE", 20.0)])
    assert main(["fix-sinr", "--db", str(path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "2 dikoreksi" in out and "--dry-run" in out and "Peringatan" in out
    assert _sinr(path) == [25.0, 20.0]
    assert main(["fix-sinr", "--db", str(path), "--serial", "HP1"]) == 0
    assert "Backup:" in capsys.readouterr().out
    assert _sinr(path) == [2.5, 2.0]
    assert main(["fix-sinr", "--db", str(tmp_path / "tidak-ada.db")]) == 1
