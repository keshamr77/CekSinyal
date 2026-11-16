import sqlite3

import pytest

from ceksinyal import db
from ceksinyal.__main__ import main
from ceksinyal.fix_band import fix_band


def _db(path, rows):
    conn = db.connect(path)
    for rat, earfcn, band in rows:
        db.insert_measurement(conn, {"ts": "t", "kos": "k", "device_serial": "HP1", "rat": rat,
                                     "earfcn": earfcn, "band": band, "raw_ok": 1})
    conn.close()
    return path


def _bands(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT band FROM measurements ORDER BY id")]
    finally:
        conn.close()


ROWS = [
    ("LTE", 1850, 1),      # kasus Infinix: salah -> 3
    ("LTE", 1850, 1),
    ("LTE", 38750, None),  # kosong -> 40
    ("LTE", 1850, 3),      # sudah benar
    ("LTE", 66500, 66),    # EARFCN di luar tabel: dibiarkan
    ("LTE", None, 1),      # tanpa EARFCN: dibiarkan
    ("NR", 630000, 78),    # bukan LTE: dibiarkan
    ("NONE", None, None),
]


def test_dry_run_writes_nothing(tmp_path):
    path = _db(tmp_path / "a.db", ROWS)
    result = fix_band(path, dry_run=True)
    assert (result.checked, result.corrected, result.written, result.backup) == (5, 3, False, None)
    assert dict(result.pairs) == {(1, 3): 2, (None, 40): 1}
    assert _bands(path) == [r[2] for r in ROWS]
    assert list(tmp_path.glob("*.backup-*")) == []


def test_write_with_backup(tmp_path):
    path = _db(tmp_path / "a.db", ROWS)
    result = fix_band(path)
    assert result.written and result.corrected == 3
    assert _bands(path) == [3, 3, 40, 3, 66, 1, 78, None]
    assert result.backup.is_file() and _bands(result.backup) == [r[2] for r in ROWS]
    again = fix_band(path)
    assert again.corrected == 0 and again.backup is None


def test_missing_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        fix_band(tmp_path / "tidak-ada.db")


def test_cli(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CEKSINYAL_CONFIG", raising=False)
    path = _db(tmp_path / "a.db", ROWS)
    assert main(["fix-band", "--db", str(path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "3 band tidak cocok" in out and "--dry-run" in out
    assert _bands(path) == [r[2] for r in ROWS]
    assert main(["fix-band", "--db", str(path)]) == 0
    assert "Backup:" in capsys.readouterr().out
    assert _bands(path)[:3] == [3, 3, 40]
    assert main(["fix-band", "--db", str(tmp_path / "tidak-ada.db")]) == 1
