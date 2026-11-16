"""Perbaikan kolom band LTE lama yang tidak cocok dengan EARFCN.

Parser lama mengambil tiap kolom identitas sel dari blok yang berbeda, sehingga band bisa berasal
dari blok yang salah (Infinix Android 14: mCellIdentity menulis mBands=[1] untuk sel EARFCN 1850,
padahal itu Band 3). EARFCN menentukan band secara pasti, jadi band dihitung ulang dari EARFCN.
Baris dengan EARFCN kosong atau di luar tabel LTE_BANDS tidak disentuh.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .collector.parser import lte_band_from_earfcn
from .fix_sinr import backup, backup_path

log = logging.getLogger(__name__)


@dataclass
class BandFixResult:
    checked: int = 0
    updates: list[tuple[int, int]] = field(default_factory=list)  # (id, band baru)
    pairs: Counter = field(default_factory=Counter)  # (band lama, band baru) -> jumlah baris
    backup: Path | None = None
    written: bool = False

    @property
    def corrected(self) -> int:
        return len(self.updates)


def plan(conn: sqlite3.Connection) -> BandFixResult:
    """Hitung koreksi tanpa menulis apa pun."""
    result = BandFixResult()
    rows = conn.execute(
        "SELECT id, earfcn, band FROM measurements WHERE rat = 'LTE' AND earfcn IS NOT NULL ORDER BY id"
    )
    for row_id, earfcn, band in rows:
        result.checked += 1
        expected = lte_band_from_earfcn(earfcn)
        if expected is not None and band != expected:
            result.updates.append((row_id, expected))
            result.pairs[(band, expected)] += 1
    return result


def fix_band(path: Path | str, dry_run: bool = False) -> BandFixResult:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"database tidak ditemukan: {path}")
    conn = sqlite3.connect(str(path))
    try:
        result = plan(conn)
    finally:
        conn.close()
    if dry_run or not result.updates:
        return result

    result.backup = backup(path, backup_path(path))
    log.info("backup database: %s", result.backup)
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        with conn:
            conn.executemany(
                "UPDATE measurements SET band = ? WHERE id = ?",
                [(band, row_id) for row_id, band in result.updates],
            )
        result.written = True
        return result
    finally:
        conn.close()
