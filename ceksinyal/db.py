"""Skema dan akses SQLite. Semua waktu disimpan sebagai UTC ISO 8601."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kos TEXT NOT NULL,
    device_serial TEXT NOT NULL,
    operator TEXT,
    rat TEXT,
    rsrp INTEGER,
    rsrq INTEGER,
    sinr REAL,
    rssi INTEGER,
    pci INTEGER,
    cell_id INTEGER,
    earfcn INTEGER,
    band INTEGER,
    sinr_raw INTEGER,
    sinr_unit TEXT,
    raw_ok INTEGER NOT NULL DEFAULT 1,
    uploaded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_measurements_operator_ts ON measurements (operator, ts);
CREATE INDEX IF NOT EXISTS idx_measurements_uploaded ON measurements (uploaded);

CREATE TABLE IF NOT EXISTS speedtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kos TEXT NOT NULL,
    operator TEXT,
    method TEXT,
    source_ip TEXT,
    ping_ms REAL,
    download_mbps REAL,
    upload_mbps REAL,
    ok INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    uploaded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_speedtests_operator_ts ON speedtests (operator, ts);

CREATE TABLE IF NOT EXISTS diagnoses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kos TEXT NOT NULL,
    operator TEXT,
    window_start TEXT,
    window_end TEXT,
    n_samples INTEGER,
    median_rsrp REAL,
    median_sinr REAL,
    median_download REAL,
    cell_changes INTEGER,
    result TEXT,
    advice TEXT,
    detail TEXT,
    uploaded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_diagnoses_operator_ts ON diagnoses (operator, ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kos TEXT NOT NULL,
    operator TEXT,
    type TEXT NOT NULL,
    detail TEXT,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
"""

MEASUREMENT_COLUMNS = (
    "ts", "kos", "device_serial", "operator", "rat", "rsrp", "rsrq", "sinr", "rssi",
    "pci", "cell_id", "earfcn", "band", "sinr_raw", "sinr_unit", "raw_ok",
)

# Kolom yang ditambahkan setelah versi awal: (tabel, kolom, tipe). Ditambahkan ke DB lama saat connect.
ADDED_COLUMNS = (
    ("measurements", "sinr_raw", "INTEGER"),  # rssnr/sinr mentah dari dumpsys
    ("measurements", "sinr_unit", "TEXT"),  # satuan sinr_raw: 'db' atau 'deci_db' (0,1 dB)
    ("diagnoses", "detail", "TEXT"),  # catatan aturan, mis. "handover: tidak tersedia"
)

EVENT_SIGNAL_LOST = "signal_lost"
EVENT_DIAGNOSIS_CHANGED = "diagnosis_changed"
EVENT_COLLECTOR_ERROR = "collector_error"
EVENT_DAILY_SUMMARY = "daily_summary"  # penanda ringkasan harian sudah dikirim (detail = tanggal lokal)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    """Buka database (membuat folder dan skema kalau belum ada)."""
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Tambahkan kolom baru ke tabel lama (tanpa menghapus data). Mengembalikan kolom yang ditambahkan."""
    added = []
    for table, column, type_ in ADDED_COLUMNS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")
            added.append(f"{table}.{column}")
    if added:
        conn.commit()
    return added


def insert_measurement(conn: sqlite3.Connection, row: dict[str, Any]) -> int:
    values = [row.get(col) for col in MEASUREMENT_COLUMNS]
    cur = conn.execute(
        f"INSERT INTO measurements ({', '.join(MEASUREMENT_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in MEASUREMENT_COLUMNS)})",
        values,
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_speedtest(conn: sqlite3.Connection, ts: str, kos: str, operator: str | None, result: Any) -> int:
    """Simpan satu hasil uji kecepatan (objek dengan atribut SpeedResult)."""
    cur = conn.execute(
        "INSERT INTO speedtests (ts, kos, operator, method, source_ip, ping_ms, download_mbps, upload_mbps, ok, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, kos, operator, result.method, result.source_ip, result.ping_ms, result.download_mbps,
         result.upload_mbps, 1 if result.ok else 0, result.error),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_event(
    conn: sqlite3.Connection, ts: str, kos: str, operator: str | None, type_: str, detail: str,
    notified: int = 0,
) -> int:
    cur = conn.execute(
        "INSERT INTO events (ts, kos, operator, type, detail, notified) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, kos, operator, type_, detail, notified),
    )
    conn.commit()
    return int(cur.lastrowid)
