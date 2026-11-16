"""DB sintetis untuk tes dashboard: dua kos, tiga HP, dengan masalah yang disengaja."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ceksinyal import db
from ceksinyal.diagnosis import engine
from ceksinyal.diagnosis.rules import Thresholds
from ceksinyal.speedtest import SpeedResult

T0 = datetime(2026, 11, 15, 8, 0, 0, tzinfo=timezone.utc)  # 15:00 WIB
END = T0 + timedelta(hours=2)
SERIAL_A, SERIAL_B, SERIAL_C = "R58MSERIALASLI", "YP7XIAOMIASLI", "INFINIX0009"


def _add(conn, start, minutes, kos, serial, operator, **fields):
    row = {"rat": "LTE", "rsrp": -90, "rsrq": -10, "sinr": 12.0, "pci": 5, "cell_id": 100, "earfcn": 1850,
           "band": 3, "raw_ok": 1, **fields}
    rows = []
    t = start
    while t < start + timedelta(minutes=minutes):
        rows.append((db.iso(t), kos, serial, operator, *[row[k] for k in (
            "rat", "rsrp", "rsrq", "sinr", "pci", "cell_id", "earfcn", "band", "raw_ok")]))
        t += timedelta(seconds=10)
    conn.executemany(
        "INSERT INTO measurements (ts, kos, device_serial, operator, rat, rsrp, rsrq, sinr, pci, cell_id, earfcn, band, raw_ok) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()


def make_db(path: Path) -> Path:
    """Garden House: Telkomsel (baik, lalu putus 10 menit) dan Tri (HP tanpa cell_id, lemah, ada jeda 20 menit).
    Kos Melati: XL (interferensi)."""
    conn = db.connect(path)
    gh = "Garden House"
    _add(conn, T0, 100, gh, SERIAL_A, "Telkomsel")
    _add(conn, T0 + timedelta(minutes=100), 10, gh, SERIAL_A, "Telkomsel", rat="NONE", rsrp=None, rsrq=None,
         sinr=None, pci=None, cell_id=None, earfcn=None, band=None)
    _add(conn, T0 + timedelta(minutes=110), 10, gh, SERIAL_A, "Telkomsel")
    _add(conn, T0, 50, gh, SERIAL_B, "Tri", rsrp=-114, rsrq=-16, sinr=3.0, pci=None, cell_id=None)
    _add(conn, T0 + timedelta(minutes=70), 50, gh, SERIAL_B, "Tri", rsrp=-104, rsrq=-13, sinr=4.0, pci=None, cell_id=None)
    _add(conn, T0, 120, "Kos Melati", SERIAL_C, "XL", rsrp=-97, rsrq=-14, sinr=-3.0)
    for i in range(8):
        ts = db.iso(T0 + timedelta(minutes=15 * i))
        db.insert_speedtest(conn, ts, gh, "Telkomsel", SpeedResult("full" if i == 4 else "light", "192.168.42.129", 35.0 + i, 20.0 + i, 6.0 if i == 4 else None, True))
        db.insert_speedtest(conn, ts, gh, "Tri", SpeedResult("light", "192.168.43.10", 80.0, 1.5 if i >= 6 else 6.0, None, True))
    db.insert_speedtest(conn, db.iso(T0 + timedelta(minutes=5)), "Kos Melati", "XL",
                        SpeedResult("light", "10.0.0.5", error="IP sumber 10.0.0.5 tidak ada di laptop (USB tethering mati atau IP berubah)"))
    db.insert_event(conn, db.iso(T0 + timedelta(minutes=55)), gh, "Tri", db.EVENT_COLLECTOR_ERROR,
                    f"HP {SERIAL_B} terputus: tidak terdeteksi oleh adb; output mentah: C:\\Users\\budi\\data\\raw\\x.txt")
    db.insert_event(conn, db.iso(T0), gh, None, db.EVENT_DAILY_SUMMARY, "2026-11-14", notified=1)
    engine.run_pending(conn, Thresholds(), now=END + timedelta(minutes=1))
    conn.close()
    return path
