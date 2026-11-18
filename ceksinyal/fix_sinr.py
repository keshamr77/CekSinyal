"""Perbaikan SINR LTE lama yang tercatat 10x lipat (HP Android 10 ke bawah, rssnr dalam 0,1 dB).

Parser lama menebak satuan per sampel, sehingga rssnr mentah -20..30 dari HP Android 10 tidak
dibagi 10 (mentah 25 tercatat 25 dB, padahal 2,5 dB). Baris lama tidak menyimpan nilai mentah,
jadi koreksinya memakai heuristik: nilai yang salah skala selalu bilangan bulat dari pola
kuantisasi modem (kelipatan ~0,25 dB x 10, dibulatkan), sedangkan nilai yang benar (hasil bagi
10) hampir selalu pecahan. Nilai 5, 7, dan -3 ambigu (bisa 5 dB asli atau 0,5 dB), jadi dibiarkan.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

SUSPECT_VALUES = frozenset({
    -20, -18, -17, -15, -13, -12, -10, -8, -7, -5, -3, 0, 2, 5, 7, 10, 12, 15, 17, 20, 22, 25, 27, 30,
})


def is_misscaled(sinr: float | None) -> bool:
    """True kalau nilai sinr lama kemungkinan besar masih dalam 0,1 dB."""
    if sinr is None or sinr != int(sinr):
        return False
    v = int(sinr)
    return v in SUSPECT_VALUES and (v >= 10 or v <= -8 or v in (0, 2))


@dataclass
class SerialSummary:
    serial: str
    checked: int = 0
    corrected: int = 0
    fractional: int = 0  # nilai pecahan: tanda HP ini memang mengirim 0,1 dB
    before: list[float] = field(default_factory=list)
    after: list[float] = field(default_factory=list)

    @property
    def median_before(self) -> float | None:
        return statistics.median(self.before) if self.before else None

    @property
    def median_after(self) -> float | None:
        return statistics.median(self.after) if self.after else None


@dataclass
class FixResult:
    serials: dict[str, SerialSummary]
    updates: list[tuple[int, float, int]]  # (id, sinr baru, sinr_raw)
    backup: Path | None = None
    written: bool = False

    @property
    def checked(self) -> int:
        return sum(s.checked for s in self.serials.values())

    @property
    def corrected(self) -> int:
        return len(self.updates)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def plan(conn: sqlite3.Connection, serial: str | None = None) -> FixResult:
    """Hitung koreksi tanpa menulis apa pun."""
    where = ["rat = 'LTE'", "sinr IS NOT NULL"]
    params: list[object] = []
    if _has_column(conn, "measurements", "sinr_raw"):
        where.append("sinr_raw IS NULL")  # baris baru sudah menyimpan nilai mentah
    if serial:
        # HP dual-SIM yang dibaca dua slot tersimpan sebagai "SERIAL/sim1" dan "SERIAL/sim2".
        where.append("(device_serial = ? OR device_serial LIKE ? ESCAPE '!')")
        pattern = serial.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        params += [serial, pattern + "/sim%"]
    rows = conn.execute(
        f"SELECT id, device_serial, sinr FROM measurements WHERE {' AND '.join(where)} ORDER BY id",
        params,
    ).fetchall()
    serials: dict[str, SerialSummary] = {}
    updates = []
    for row_id, dev, sinr in rows:
        s = serials.setdefault(dev, SerialSummary(dev))
        s.checked += 1
        s.before.append(sinr)
        if sinr != int(sinr):
            s.fractional += 1
        if is_misscaled(sinr):
            new = round(sinr / 10, 1)
            updates.append((row_id, new, int(sinr)))
            s.corrected += 1
            s.after.append(new)
        else:
            s.after.append(sinr)
    return FixResult(serials, updates)


def backup_path(path: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.stem}.backup-{stamp}{path.suffix or '.db'}")


def backup(path: Path, dest: Path) -> Path:
    """Salin database dengan backup API SQLite (aman untuk mode WAL)."""
    src = sqlite3.connect(str(path))
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def fix_sinr(path: Path | str, serial: str | None = None, dry_run: bool = False) -> FixResult:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"database tidak ditemukan: {path}")
    conn = sqlite3.connect(str(path))  # hanya membaca: tanpa migrasi
    try:
        result = plan(conn, serial)
    finally:
        conn.close()
    if dry_run or not result.updates:
        return result

    # Backup sebelum menulis apa pun (termasuk ALTER TABLE dari migrasi).
    result.backup = backup(path, backup_path(path))
    log.info("backup database: %s", result.backup)
    conn = db.connect(path)  # menambahkan kolom sinr_raw/sinr_unit ke DB lama
    try:
        with conn:
            conn.executemany(
                "UPDATE measurements SET sinr = ?, sinr_raw = ?, sinr_unit = 'deci_db' WHERE id = ?",
                [(new, raw, row_id) for row_id, new, raw in result.updates],
            )
        result.written = True
        return result
    finally:
        conn.close()
