"""Uploader Google Sheets dengan store-and-forward.

Baris `uploaded=0` dikirim per batch, lalu ditandai `uploaded=1` HANYA setelah Sheets menerima
batch itu. Kalau internet putus atau API menolak, baris tetap 0 dan dicoba lagi di putaran berikut,
jadi tidak ada data yang hilang. Jaminannya "minimal sekali": kalau koneksi putus tepat setelah
Sheets menerima tetapi sebelum jawabannya sampai, batch itu bisa terkirim dua kali.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from .. import db
from ..config import Config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
MAX_BATCHES_PER_CYCLE = 20  # batas per putaran supaya tidak menabrak kuota API (60 tulis/menit)

RAW_MEASUREMENT_HEADERS = [
    "id", "ts", "kos", "device_serial", "operator", "rat", "rsrp", "rsrq", "sinr", "rssi", "pci", "cell_id",
    "earfcn", "band", "raw_ok",
]
MINUTE_HEADERS = [
    "ts_minute", "kos", "device_serial", "operator", "n_samples", "n_no_service", "n_parse_failed", "rat",
    "rsrp_avg", "rsrp_min", "rsrq_avg", "sinr_avg", "pci", "cell_id", "earfcn", "band",
]
SPEEDTEST_HEADERS = [
    "id", "ts", "kos", "operator", "method", "source_ip", "ping_ms", "download_mbps", "upload_mbps", "ok", "error",
]
DIAGNOSIS_HEADERS = [
    "id", "ts", "kos", "operator", "window_start", "window_end", "n_samples", "median_rsrp", "median_sinr",
    "median_download", "cell_changes", "result", "advice", "detail",
]


class SheetsBackend(Protocol):
    def append(self, tab: str, headers: list[str], rows: list[list[Any]]) -> None:
        """Tambahkan baris ke tab (membuat tab dan baris judul kalau belum ada). Melempar kalau gagal."""


class GspreadBackend:
    """Akses spreadsheet lewat gspread + service account. gspread diimpor saat pertama dipakai."""

    def __init__(self, spreadsheet_id: str, credentials_file: Path) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.credentials_file = Path(credentials_file)
        self._spreadsheet = None
        self._worksheets: dict[str, Any] = {}

    def _open(self):
        if self._spreadsheet is None:
            import gspread

            client = gspread.service_account(filename=str(self.credentials_file), scopes=SCOPES)
            self._spreadsheet = client.open_by_key(self.spreadsheet_id)
        return self._spreadsheet

    def check(self) -> str:
        """Buka spreadsheet dan kembalikan judulnya (untuk selftest)."""
        return str(self._open().title)

    def _worksheet(self, tab: str, headers: list[str]):
        if tab not in self._worksheets:
            import gspread

            spreadsheet = self._open()
            try:
                ws = spreadsheet.worksheet(tab)
            except gspread.WorksheetNotFound:
                ws = spreadsheet.add_worksheet(title=tab, rows=1000, cols=len(headers))
            if not ws.row_values(1):
                ws.append_row(headers, value_input_option="RAW")
            self._worksheets[tab] = ws
        return self._worksheets[tab]

    def append(self, tab: str, headers: list[str], rows: list[list[Any]]) -> None:
        try:
            self._worksheet(tab, headers).append_rows(rows, value_input_option="RAW")
        except Exception:
            # Koneksi/token bisa basi setelah internet putus: buka ulang di percobaan berikutnya.
            self._spreadsheet = None
            self._worksheets.clear()
            raise


def _cell(value: Any) -> Any:
    return "" if value is None else value


def _avg(values: list[float]) -> float | str:
    return round(statistics.fmean(values), 1) if values else ""


def aggregate_minutes(rows: list[sqlite3.Row]) -> list[tuple[list[Any], list[int]]]:
    """Rata-rata per menit per HP. Mengembalikan [(baris sheet, id baris sumber)], urut waktu."""
    groups: dict[tuple, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault((r["ts"][:16], r["kos"], r["device_serial"], r["operator"]), []).append(r)
    out = []
    for (minute, kos, serial, operator), items in sorted(groups.items(), key=lambda kv: [str(x) for x in kv[0]]):
        good = [r for r in items if r["raw_ok"]]
        served = [r for r in good if r["rat"] not in (None, "NONE")]

        def values(name: str) -> list[float]:
            return [r[name] for r in served if r[name] is not None]

        def last(name: str) -> Any:
            return next((r[name] for r in reversed(served) if r[name] is not None), "")

        rats = Counter(r["rat"] for r in served)
        rat = rats.most_common(1)[0][0] if rats else ("NONE" if good else "")
        rsrp = values("rsrp")
        row = [
            f"{minute}:00+00:00", kos, serial, _cell(operator), len(good), len(good) - len(served),
            len(items) - len(good), rat, _avg(rsrp), min(rsrp) if rsrp else "", _avg(values("rsrq")),
            _avg(values("sinr")), last("pci"), last("cell_id"), last("earfcn"), last("band"),
        ]
        out.append((row, [r["id"] for r in items]))
    return out


class Uploader:
    def __init__(
        self, config: Config, conn: sqlite3.Connection, backend: SheetsBackend,
        now: Callable[[], datetime] = db.utc_now,
    ) -> None:
        self.config = config
        self.conn = conn
        self.backend = backend
        self._now = now

    # ------------------------------------------------------------ batch per tabel

    def _mark(self, table: str, ids: list[int]) -> None:
        with self.conn:
            self.conn.executemany(f"UPDATE {table} SET uploaded = 1 WHERE id = ?", [(i,) for i in ids])

    def _simple_batch(self, table: str, headers: list[str]) -> tuple[list[list[Any]], list[int]]:
        rows = self.conn.execute(
            f"SELECT {', '.join(headers)} FROM {table} WHERE uploaded = 0 ORDER BY id LIMIT ?",
            (self.config.sheets.batch_size,),
        ).fetchall()
        return [[_cell(v) for v in r] for r in rows], [r["id"] for r in rows]

    def _minute_batch(self) -> tuple[list[list[Any]], list[int]]:
        # Menit yang sedang berjalan belum dikirim, supaya satu menit tidak terkirim dua kali sepotong-sepotong.
        current_minute = db.iso(self._now())[:16]
        minutes = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT substr(ts, 1, 16) AS m FROM measurements WHERE uploaded = 0 AND substr(ts, 1, 16) < ? "
            "ORDER BY m LIMIT ?", (current_minute, self.config.sheets.batch_size),
        )]
        if not minutes:
            return [], []
        rows = self.conn.execute(
            "SELECT * FROM measurements WHERE uploaded = 0 AND substr(ts, 1, 16) BETWEEN ? AND ? ORDER BY ts, id",
            (minutes[0], minutes[-1]),
        ).fetchall()
        aggregated = aggregate_minutes(rows)
        return [row for row, _ in aggregated], [i for _, ids in aggregated for i in ids]

    def _upload_table(self, table: str) -> int:
        """Kirim semua batch tertunda satu tabel. Mengembalikan jumlah baris sheet yang terkirim."""
        sent = 0
        for _ in range(MAX_BATCHES_PER_CYCLE):
            if table == "measurements" and self.config.sheets.aggregate_measurements:
                headers = MINUTE_HEADERS
                rows, ids = self._minute_batch()
            else:
                headers = {"measurements": RAW_MEASUREMENT_HEADERS, "speedtests": SPEEDTEST_HEADERS,
                           "diagnoses": DIAGNOSIS_HEADERS}[table]
                rows, ids = self._simple_batch(table, headers)
            if not rows:
                break
            self.backend.append(table, headers, rows)  # melempar kalau gagal -> baris tetap uploaded=0
            self._mark(table, ids)
            sent += len(rows)
        return sent

    # ------------------------------------------------------------ publik

    def pending(self) -> dict[str, int]:
        return {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE uploaded = 0").fetchone()[0]
            for table in ("measurements", "speedtests", "diagnoses")
        }

    def upload_pending(self) -> dict[str, int]:
        """Kirim baris tertunda semua tabel. Tidak pernah melempar; kegagalan dicatat dan dicoba lagi nanti."""
        sent: dict[str, int] = {}
        for table in ("measurements", "speedtests", "diagnoses"):
            try:
                sent[table] = self._upload_table(table)
            except Exception as exc:
                log.warning("upload %s gagal (dicoba lagi nanti, data tetap tersimpan lokal): %s", table, exc)
                sent[table] = -1
                break  # kemungkinan besar internet/API bermasalah: tabel lain menunggu putaran berikut
        done = {k: v for k, v in sent.items() if v > 0}
        if done:
            log.info("upload Sheets: %s", ", ".join(f"{k} {v} baris" for k, v in done.items()))
        return sent


def sheets_problem(config: Config) -> str | None:
    """Alasan uploader Sheets tidak aktif, atau None kalau siap."""
    s = config.sheets
    if not s.enabled:
        return "sheets.enabled: false di config"
    if not s.spreadsheet_id:
        return "spreadsheet_id belum diisi (config sheets.spreadsheet_id atau GOOGLE_SPREADSHEET_ID di .env)"
    if not s.credentials_file:
        return "file kredensial belum diisi (GOOGLE_CREDENTIALS_FILE di .env)"
    if not s.credentials_file.is_file():
        return f"file kredensial tidak ditemukan: {s.credentials_file}"
    return None


def make_uploader(config: Config, conn: sqlite3.Connection) -> Uploader | None:
    """Uploader dari config, atau None kalau Sheets tidak dikonfigurasi."""
    if not config.sheets.active:
        return None
    return Uploader(config, conn, GspreadBackend(config.sheets.spreadsheet_id, config.sheets.credentials_file))
