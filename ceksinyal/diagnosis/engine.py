"""Mesin diagnosis: memotong data jadi jendela, menjalankan aturan, mengisi tabel diagnoses dan events.

Jendela berada di kisi tetap (kelipatan `window_minutes` sejak epoch UTC), jadi hasilnya sama antara
mode langsung dan `--rerun`. Jumlah sampel dihitung dari baris nyata di jendela itu; collector yang
berhenti sebentar menghasilkan jendela "Data kurang", bukan diagnosis palsu.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import db
from .rules import INSUFFICIENT, LOST, NORMAL, PROBLEMS, SEVERITY, Sample, Thresholds, WindowResult, evaluate

log = logging.getLogger(__name__)

# Event dari jendela yang lebih tua dari ini langsung ditandai notified=1 (tidak di-alert bot).
NOTIFY_MAX_AGE = timedelta(minutes=30)
DAY = timedelta(hours=24)


@dataclass
class DiagnosisRow:
    kos: str
    operator: str
    window_start: datetime
    window_end: datetime
    window: WindowResult


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def floor_window(dt: datetime, minutes: float) -> datetime:
    size = int(minutes * 60)
    epoch = int(dt.timestamp())
    return datetime.fromtimestamp(epoch - epoch % size, tz=timezone.utc)


def _speeds(conn: sqlite3.Connection, kos: str, operator: str, start: datetime, end: datetime) -> list[float]:
    rows = conn.execute(
        "SELECT download_mbps FROM speedtests WHERE kos = ? AND operator = ? AND ok = 1 "
        "AND download_mbps IS NOT NULL AND ts >= ? AND ts < ? ORDER BY ts, id",
        (kos, operator, db.iso(start), db.iso(end)),
    )
    return [float(r[0]) for r in rows]


def diagnose_window(
    conn: sqlite3.Connection, kos: str, operator: str, start: datetime, end: datetime, th: Thresholds
) -> WindowResult:
    rows = conn.execute(
        "SELECT rat, rsrp, sinr, cell_id, earfcn, raw_ok FROM measurements "
        "WHERE kos = ? AND operator = ? AND ts >= ? AND ts < ? ORDER BY ts, id",
        (kos, operator, db.iso(start), db.iso(end)),
    ).fetchall()
    samples = [Sample(r[0], r[1], r[2], r[3], r[4]) for r in rows if r[5]]
    failed = len(rows) - len(samples)

    window_speeds = _speeds(conn, kos, operator, start, end)
    if not window_speeds:
        # Uji ringan tiap 15 menit, jendela 10 menit: pakai uji terakhir yang masih dekat.
        recent = _speeds(conn, kos, operator, end - timedelta(minutes=th.speed_lookback_minutes), end)
        window_speeds = recent[-1:]
    daily_speeds = _speeds(conn, kos, operator, end - DAY, end)

    result = evaluate(samples, window_speeds, daily_speeds, th)
    if failed:
        result.notes.append(f"{failed} pembacaan gagal di-parse (tidak dihitung)")
    return result


def _groups(conn: sqlite3.Connection, kos: str | None) -> list[tuple[str, str, datetime, datetime]]:
    sql = "SELECT kos, operator, MIN(ts), MAX(ts) FROM measurements WHERE operator IS NOT NULL"
    params: list[object] = []
    if kos:
        sql += " AND kos = ?"
        params.append(kos)
    sql += " GROUP BY kos, operator ORDER BY kos, operator"
    return [(r[0], r[1], parse_ts(r[2]), parse_ts(r[3])) for r in conn.execute(sql, params)]


def _insert(conn: sqlite3.Connection, row: DiagnosisRow, now: datetime) -> None:
    w = row.window
    conn.execute(
        "INSERT INTO diagnoses (ts, kos, operator, window_start, window_end, n_samples, median_rsrp, "
        "median_sinr, median_download, cell_changes, result, advice, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (db.iso(now), row.kos, row.operator, db.iso(row.window_start), db.iso(row.window_end), w.n_samples,
         w.median_rsrp, w.median_sinr, w.median_download, w.cell_changes, w.result, w.advice, w.detail),
    )


def _fmt(value: float | None, unit: str) -> str:
    return "-" if value is None else f"{value:g} {unit}"


def _emit_event(conn: sqlite3.Connection, row: DiagnosisRow, prev: str | None, now: datetime) -> None:
    """Catat perubahan diagnosis. "Data kurang" tidak dianggap perubahan (pembanding = hasil konklusif terakhir)."""
    w = row.window
    if w.result == INSUFFICIENT or w.result == prev:
        return
    if prev is None and w.result == NORMAL:
        return  # diagnosis pertama dan normal: bukan kejadian
    summary = (
        f"jendela {db.iso(row.window_start)}..{db.iso(row.window_end)}, {w.n_samples} sampel, "
        f"median RSRP {_fmt(w.median_rsrp, 'dBm')}, median SINR {_fmt(w.median_sinr, 'dB')}"
    )
    if w.result == LOST:
        type_, detail = db.EVENT_SIGNAL_LOST, f"{prev or '-'} -> {LOST}; {summary}"
    else:
        type_, detail = db.EVENT_DIAGNOSIS_CHANGED, f"{prev or '-'} -> {w.result}; {summary}"
    notified = 1 if now - row.window_end > NOTIFY_MAX_AGE else 0
    conn.execute(
        "INSERT INTO events (ts, kos, operator, type, detail, notified) VALUES (?, ?, ?, ?, ?, ?)",
        (db.iso(row.window_end), row.kos, row.operator, type_, detail, notified),
    )


def run_pending(
    conn: sqlite3.Connection, th: Thresholds, now: datetime | None = None, kos: str | None = None
) -> list[DiagnosisRow]:
    """Diagnosis semua jendela selesai yang belum didiagnosis, per (kos, operator).

    Mulai dari jendela setelah diagnosis terakhir (atau dari sampel pertama), sampai jendela yang
    memuat sampel terakhir, dan tidak melewati `now`. Jendela kosong di tengah ikut dicatat.
    """
    now = now or db.utc_now()
    size = timedelta(minutes=th.window_minutes)
    written: list[DiagnosisRow] = []
    for g_kos, operator, first_ts, last_ts in _groups(conn, kos):
        last = conn.execute(
            "SELECT MAX(window_end) FROM diagnoses WHERE kos = ? AND operator = ?", (g_kos, operator)
        ).fetchone()[0]
        start = parse_ts(last) if last else floor_window(first_ts, th.window_minutes)
        stop = min(floor_window(now, th.window_minutes), floor_window(last_ts, th.window_minutes) + size)
        prev_row = conn.execute(
            "SELECT result FROM diagnoses WHERE kos = ? AND operator = ? AND result != ? "
            "ORDER BY window_end DESC LIMIT 1", (g_kos, operator, INSUFFICIENT),
        ).fetchone()
        prev = prev_row[0] if prev_row else None
        while start + size <= stop:
            end = start + size
            row = DiagnosisRow(g_kos, operator, start, end, diagnose_window(conn, g_kos, operator, start, end, th))
            with conn:
                _insert(conn, row, now)
                _emit_event(conn, row, prev, now)
            if row.window.result != INSUFFICIENT:
                prev = row.window.result
            written.append(row)
            start = end
    if written:
        log.info("diagnosis: %d jendela baru", len(written))
    return written


def rerun(
    conn: sqlite3.Connection, th: Thresholds, now: datetime | None = None, kos: str | None = None
) -> list[DiagnosisRow]:
    """Hapus diagnosis (dan event turunannya) lalu hitung ulang dari data lama, mis. setelah ambang diubah."""
    where, params = ("WHERE kos = ?", [kos]) if kos else ("", [])
    with conn:
        conn.execute(f"DELETE FROM diagnoses {where}", params)
        conn.execute(
            f"DELETE FROM events {where or 'WHERE 1=1'} AND type IN (?, ?)",
            [*params, db.EVENT_SIGNAL_LOST, db.EVENT_DIAGNOSIS_CHANGED],
        )
    return run_pending(conn, th, now, kos)


# ---------------------------------------------------------------- rekomendasi operator

@dataclass
class OperatorSummary:
    operator: str
    windows: int               # jendela konklusif (bukan "Data kurang")
    normal_share: float | None  # porsi jendela konklusif yang Normal
    dominant: str | None       # diagnosis terbanyak (seri -> yang lebih parah)
    median_rsrp: float | None
    counts: dict[str, int]


@dataclass
class Recommendation:
    kos: str
    best: str | None
    reason: str
    operators: list[OperatorSummary]


def summarize_operators(
    conn: sqlite3.Connection, kos: str, since: datetime | None = None, until: datetime | None = None
) -> list[OperatorSummary]:
    lo = db.iso(since) if since else ""
    hi = db.iso(until) if until else "9999"
    counts: dict[str, Counter] = {}
    for operator, result, n in conn.execute(
        "SELECT operator, result, COUNT(*) FROM diagnoses WHERE kos = ? AND window_end > ? AND window_start < ? "
        "GROUP BY operator, result", (kos, lo, hi),
    ):
        counts.setdefault(operator, Counter())[result] = n
    out = []
    for operator, c in sorted(counts.items()):
        conclusive = {r: n for r, n in c.items() if r != INSUFFICIENT}
        total = sum(conclusive.values())
        rsrp = [r[0] for r in conn.execute(
            "SELECT rsrp FROM measurements WHERE kos = ? AND operator = ? AND raw_ok = 1 AND rsrp IS NOT NULL "
            "AND ts >= ? AND ts < ?", (kos, operator, lo, hi),
        )]
        dominant = None
        if conclusive:
            dominant = max(conclusive, key=lambda r: (conclusive[r], SEVERITY.index(r) if r in SEVERITY else -1))
        out.append(OperatorSummary(
            operator=operator,
            windows=total,
            normal_share=(conclusive.get(NORMAL, 0) / total) if total else None,
            dominant=dominant,
            median_rsrp=float(statistics.median(rsrp)) if rsrp else None,
            counts=dict(c),
        ))
    return out


def recommend(
    conn: sqlite3.Connection, kos: str, since: datetime | None = None, until: datetime | None = None
) -> Recommendation:
    """Operator terbaik di satu kos: porsi jendela Normal tertinggi, lalu median RSRP tertinggi."""
    ops = summarize_operators(conn, kos, since, until)
    ranked = [o for o in ops if o.windows > 0]
    if len(ranked) < 2:
        reason = (
            "Rekomendasi operator butuh minimal dua operator dengan diagnosis di kos ini "
            f"(tersedia: {len(ranked)})."
        )
        return Recommendation(kos, None, reason, ops)
    ranked.sort(key=lambda o: (-(o.normal_share or 0), -(o.median_rsrp if o.median_rsrp is not None else -999)))
    best, second = ranked[0], ranked[1]
    if best.normal_share != second.normal_share:
        reason = (
            f"{best.operator}: {best.normal_share:.0%} jendela Normal "
            f"(dibanding {second.operator} {second.normal_share:.0%})."
        )
    else:
        reason = (
            f"Diagnosis setara ({best.normal_share:.0%} jendela Normal); {best.operator} dipilih karena median RSRP "
            f"lebih tinggi ({_fmt(best.median_rsrp, 'dBm')} dibanding {_fmt(second.median_rsrp, 'dBm')})."
        )
    return Recommendation(kos, best.operator, reason, ops)


__all__ = [
    "DiagnosisRow", "OperatorSummary", "Recommendation", "PROBLEMS", "diagnose_window", "floor_window",
    "parse_ts", "recommend", "rerun", "run_pending", "summarize_operators",
]
