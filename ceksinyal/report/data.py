"""Kumpulkan isi laporan bukti satu kos dari database (tanpa urusan tampilan)."""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .. import db
from ..collector.parser import lte_band_from_earfcn
from ..config import KOS_UNSET
from ..diagnosis import engine, rules
from ..diagnosis.rules import Thresholds
from ..fix_sinr import is_misscaled

METRICS = {  # nama kolom -> (label, satuan, lebar bin histogram)
    "rsrp": ("RSRP", "dBm", 5),
    "rsrq": ("RSRQ", "dB", 2),
    "sinr": ("SINR", "dB", 2),
}
WEAK_RSRP_DBM = -100.0  # ambang "RSRP lemah" di ringkasan (bukan ambang diagnosis)
MAX_SERIES_POINTS = 720


@dataclass
class Stats:
    n: int
    minimum: float
    p10: float
    median: float
    p90: float
    maximum: float


@dataclass
class Gap:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass
class SpeedSummary:
    n_ok: int = 0
    n_failed: int = 0
    median_download: float | None = None
    median_upload: float | None = None
    median_ping: float | None = None
    min_download: float | None = None
    max_download: float | None = None


@dataclass
class OperatorReport:
    operator: str
    serials: list[str]
    n_rows: int = 0            # semua baris, termasuk raw_ok=0
    n_samples: int = 0         # raw_ok=1
    n_lost: int = 0            # rat=NONE
    first: datetime | None = None
    last: datetime | None = None
    interval_seconds: float | None = None  # median jarak antar-sampel
    rat_counts: dict[str, int] = field(default_factory=dict)
    band_counts: dict[str, int] = field(default_factory=dict)
    stats: dict[str, Stats | None] = field(default_factory=dict)
    histograms: dict[str, list[tuple[float, float, int]]] = field(default_factory=dict)
    pct_weak_rsrp: float | None = None
    series: list[tuple[datetime, float | None]] = field(default_factory=list)  # RSRP per waktu; None = putus garis
    diag_counts: dict[str, int] = field(default_factory=dict)
    dominant: str | None = None
    speed: SpeedSummary = field(default_factory=SpeedSummary)
    gaps: list[Gap] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    @property
    def pct_lost(self) -> float | None:
        return 100 * self.n_lost / self.n_samples if self.n_samples else None

    @property
    def hours(self) -> float:
        return (self.last - self.first).total_seconds() / 3600 if self.first and self.last else 0.0


@dataclass
class DiagnosisWindow:
    operator: str
    start: datetime
    end: datetime
    n_samples: int
    median_rsrp: float | None
    median_sinr: float | None
    median_download: float | None
    cell_changes: int | None
    result: str
    detail: str


@dataclass
class Event:
    ts: datetime
    operator: str | None
    type: str
    detail: str


@dataclass
class ReportData:
    kos: str
    generated: datetime
    tz: timezone
    tz_label: str
    first: datetime | None
    last: datetime | None
    thresholds: Thresholds
    operators: list[OperatorReport]
    windows: list[DiagnosisWindow]
    lost_events: list[Event]
    collector_events: list[Event]
    recommendation: engine.Recommendation
    notes: list[str]

    @property
    def lost_windows(self) -> list[DiagnosisWindow]:
        return [w for w in self.windows if w.result == rules.LOST]


def list_kos(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [(r[0], r[1]) for r in conn.execute("SELECT kos, COUNT(*) FROM measurements GROUP BY kos ORDER BY kos")]


def _stats(values: list[float]) -> Stats | None:
    if not values:
        return None
    ordered = sorted(values)

    def pct(p: float) -> float:
        k = (len(ordered) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)

    return Stats(len(ordered), ordered[0], pct(0.1), float(statistics.median(ordered)), pct(0.9), ordered[-1])


def histogram(values: list[float], width: float) -> list[tuple[float, float, int]]:
    """Bin [lo, lo+width) dari nilai terkecil sampai terbesar; bin kosong di tengah tetap ada."""
    if not values:
        return []
    start = math.floor(min(values) / width) * width
    counts = Counter(math.floor((v - start) / width) for v in values)
    return [(start + i * width, start + (i + 1) * width, counts.get(i, 0)) for i in range(max(counts) + 1)]


def _series(points: list[tuple[datetime, float | None]], gap_seconds: float) -> list[tuple[datetime, float | None]]:
    """Deret RSRP untuk grafik: diringkas (median per ember) kalau terlalu panjang, None di tiap jeda."""
    if len(points) > MAX_SERIES_POINTS:
        t0, t1 = points[0][0], points[-1][0]
        size = max((t1 - t0).total_seconds() / MAX_SERIES_POINTS, 1)
        buckets: dict[int, list[float]] = {}
        for ts, value in points:
            if value is not None:
                buckets.setdefault(int((ts - t0).total_seconds() // size), []).append(value)
        points = [(t0 + timedelta(seconds=(i + 0.5) * size), float(statistics.median(v)))
                  for i, v in sorted(buckets.items())]
        gap_seconds = max(gap_seconds, 3 * size)
    out: list[tuple[datetime, float | None]] = []
    prev = None
    for ts, value in points:
        if prev is not None and (ts - prev).total_seconds() > gap_seconds:
            out.append((prev + (ts - prev) / 2, None))
        out.append((ts, value))
        prev = ts
    return out


def _operator_report(
    conn: sqlite3.Connection, kos: str, operator: str, lo: str, hi: str, th: Thresholds
) -> OperatorReport:
    rows = conn.execute(
        "SELECT ts, device_serial, rat, rsrp, rsrq, sinr, pci, cell_id, earfcn, band, sinr_raw, raw_ok "
        "FROM measurements WHERE kos = ? AND operator = ? AND ts >= ? AND ts < ? ORDER BY ts, id",
        (kos, operator, lo, hi),
    ).fetchall()
    rep = OperatorReport(operator, sorted({r["device_serial"] for r in rows}))
    rep.n_rows = len(rows)
    ok = [r for r in rows if r["raw_ok"]]
    rep.n_samples = len(ok)
    if not ok:
        rep.limitations.append("Tidak ada sampel yang berhasil di-parse pada periode ini.")
        return rep
    times = [engine.parse_ts(r["ts"]) for r in ok]
    rep.first, rep.last = times[0], times[-1]
    deltas = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    rep.interval_seconds = float(statistics.median(deltas)) if deltas else None
    rep.n_lost = sum(1 for r in ok if r["rat"] == "NONE")
    rep.rat_counts = dict(Counter(r["rat"] or "?" for r in ok).most_common())
    served = [r for r in ok if r["rat"] not in (None, "NONE")]
    rep.band_counts = dict(Counter(
        f"{r['rat']} B{r['band']}" if r["band"] is not None else f"{r['rat']} band ?" for r in served
    ).most_common())

    for name, (_, _, width) in METRICS.items():
        values = [float(r[name]) for r in served if r[name] is not None]
        rep.stats[name] = _stats(values)
        rep.histograms[name] = histogram(values, width)
    rsrp = [r["rsrp"] for r in served if r["rsrp"] is not None]
    if rsrp:
        rep.pct_weak_rsrp = 100 * sum(1 for v in rsrp if v < WEAK_RSRP_DBM) / len(rsrp)

    gap_limit = max(60.0, 6 * (rep.interval_seconds or 10))
    rep.gaps = [Gap(a, b) for a, b in zip(times, times[1:]) if (b - a).total_seconds() > gap_limit]
    rep.series = _series(
        [(t, float(r["rsrp"]) if r["rsrp"] is not None else None) for t, r in zip(times, ok)], gap_limit
    )

    counts = Counter(r[0] for r in conn.execute(
        "SELECT result FROM diagnoses WHERE kos = ? AND operator = ? AND window_end > ? AND window_start < ?",
        (kos, operator, lo, hi),
    ))
    rep.diag_counts = dict(counts)
    conclusive = {k: v for k, v in counts.items() if k != rules.INSUFFICIENT}
    if conclusive:
        rep.dominant = max(conclusive, key=lambda k: (conclusive[k], rules.SEVERITY.index(k) if k in rules.SEVERITY else -1))

    speeds = conn.execute(
        "SELECT ok, download_mbps, upload_mbps, ping_ms FROM speedtests WHERE kos = ? AND operator = ? "
        "AND ts >= ? AND ts < ?", (kos, operator, lo, hi),
    ).fetchall()
    good = [s for s in speeds if s["ok"]]
    sp = rep.speed
    sp.n_ok, sp.n_failed = len(good), len(speeds) - len(good)

    def med(key: str) -> float | None:
        values = [s[key] for s in good if s[key] is not None]
        return float(statistics.median(values)) if values else None

    sp.median_download, sp.median_upload, sp.median_ping = med("download_mbps"), med("upload_mbps"), med("ping_ms")
    downloads = [s["download_mbps"] for s in good if s["download_mbps"] is not None]
    if downloads:
        sp.min_download, sp.max_download = min(downloads), max(downloads)

    rep.limitations = _limitations(rep, rows, served, th)
    return rep


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def _limitations(rep: OperatorReport, rows: list, served: list, th: Thresholds) -> list[str]:
    """Catatan keterbatasan data HP ini, supaya pembaca tahu angka mana yang tidak bisa diandalkan."""
    notes = []
    n = len(served)
    failed = rep.n_rows - rep.n_samples
    if failed:
        notes.append(f"{failed} dari {rep.n_rows} pembacaan gagal di-parse (raw_ok=0) dan tidak dihitung.")
    if n:
        have = {k: sum(1 for r in served if r[k] is not None) for k in ("cell_id", "pci", "earfcn", "sinr", "rsrq")}
        if have["cell_id"] == 0:
            if have["earfcn"] == 0:
                notes.append("HP tidak memberikan cell_id maupun EARFCN: aturan handover tidak bisa dievaluasi.")
            else:
                notes.append(
                    f"HP menyembunyikan cell_id (dan PCI terisi {_pct(have['pci'], n)}); handover dihitung dari "
                    f"pergantian EARFCN, yang hanya terisi di {_pct(have['earfcn'], n)} sampel"
                    + (" (jumlah pergantian adalah batas bawah)." if have["earfcn"] * 2 < n else ".")
                )
        elif have["cell_id"] < n:
            notes.append(f"cell_id hanya terisi di {_pct(have['cell_id'], n)} sampel.")
        if have["sinr"] == 0:
            notes.append("HP tidak memberikan SINR: aturan interferensi tidak bisa dievaluasi.")
        else:
            sinr = [r["sinr"] for r in served if r["sinr"] is not None]
            if len(sinr) >= th.min_metric_samples and all(v == 0 for v in sinr):
                notes.append("SINR selalu persis 0: kemungkinan nilai kosong dari HP, bukan 0 dB sungguhan. "
                             "SINR tidak dipakai untuk diagnosis.")
            elif have["sinr"] < n:
                notes.append(f"SINR hanya terisi di {_pct(have['sinr'], n)} sampel.")
        lte = [r for r in served if r["rat"] == "LTE" and r["sinr"] is not None]
        legacy = [r for r in lte if r["sinr_raw"] is None]
        if legacy:
            suspect = sum(1 for r in legacy if is_misscaled(r["sinr"]))
            text = (f"{len(legacy)} sampel LTE direkam dengan versi lama (tanpa nilai SINR mentah), "
                    "jadi satuan SINR-nya tidak bisa diperiksa ulang")
            if suspect:
                text += (f"; {suspect} di antaranya berpola salah skala 10x. Jalankan `fix-sinr` lalu "
                         "`diagnose --rerun` sebelum memakai angka SINR")
            notes.append(text + ".")
        wrong_band = sum(
            1 for r in served
            if r["rat"] == "LTE" and r["earfcn"] is not None
            and lte_band_from_earfcn(r["earfcn"]) not in (None, r["band"])
        )
        if wrong_band:
            notes.append(f"{wrong_band} sampel punya band yang tidak cocok dengan EARFCN. Jalankan `fix-band`.")
    if rep.gaps:
        total = sum(g.minutes for g in rep.gaps)
        notes.append(f"Data bolong {len(rep.gaps)} kali (total {total:.0f} menit tanpa sampel); "
                     "jendela diagnosis di rentang itu menjadi \"Data kurang\".")
    if rep.speed.n_ok == 0:
        notes.append("Tidak ada uji kecepatan yang berhasil: aturan sel padat tidak bisa dievaluasi.")
    elif rep.speed.n_ok < th.min_speed_samples:
        notes.append(f"Baru {rep.speed.n_ok} uji kecepatan (minimal {th.min_speed_samples} untuk aturan sel padat).")
    if rep.hours < 1:
        notes.append(f"Rekaman hanya {rep.hours * 60:.0f} menit; belum mewakili variasi sepanjang hari.")
    if len(rep.serials) > 1:
        notes.append(f"Operator ini dibaca dari {len(rep.serials)} HP berbeda ({', '.join(rep.serials)}).")
    return notes


def build(
    conn: sqlite3.Connection, kos: str, th: Thresholds, tz: timezone, tz_label: str,
    since: datetime | None = None, until: datetime | None = None, now: datetime | None = None,
) -> ReportData:
    lo = db.iso(since) if since else ""
    hi = db.iso(until) if until else "9999"
    operators = [r[0] for r in conn.execute(
        "SELECT DISTINCT operator FROM measurements WHERE kos = ? AND operator IS NOT NULL AND ts >= ? AND ts < ? "
        "ORDER BY operator", (kos, lo, hi),
    )]
    reports = [_operator_report(conn, kos, op, lo, hi, th) for op in operators]
    windows = [
        DiagnosisWindow(r["operator"], engine.parse_ts(r["window_start"]), engine.parse_ts(r["window_end"]),
                        r["n_samples"] or 0, r["median_rsrp"], r["median_sinr"], r["median_download"],
                        r["cell_changes"], r["result"], r["detail"] or "")
        for r in conn.execute(
            "SELECT * FROM diagnoses WHERE kos = ? AND window_end > ? AND window_start < ? "
            "ORDER BY window_start, operator", (kos, lo, hi),
        )
    ]

    def events(*types: str) -> list[Event]:
        marks = ", ".join("?" for _ in types)
        return [Event(engine.parse_ts(r["ts"]), r["operator"], r["type"], r["detail"] or "") for r in conn.execute(
            f"SELECT * FROM events WHERE kos = ? AND ts >= ? AND ts < ? AND type IN ({marks}) ORDER BY ts, id",
            (kos, lo, hi, *types),
        )]

    firsts = [r.first for r in reports if r.first]
    lasts = [r.last for r in reports if r.last]
    notes = []
    if kos == KOS_UNSET:
        notes.append("Nama kos belum diisi saat data direkam (kos = \"belum-diatur\"). Isi `kos` di config.yaml; "
                     "data lama ini tidak bisa dibandingkan dengan kos lain.")
    if not windows and reports:
        notes.append("Belum ada diagnosis untuk periode ini. Jalankan `python -m ceksinyal diagnose` dulu.")
    return ReportData(
        kos=kos, generated=now or db.utc_now(), tz=tz, tz_label=tz_label,
        first=min(firsts) if firsts else None, last=max(lasts) if lasts else None,
        thresholds=th, operators=reports, windows=windows,
        lost_events=events(db.EVENT_SIGNAL_LOST), collector_events=events(db.EVENT_COLLECTOR_ERROR),
        recommendation=engine.recommend(conn, kos, since, until), notes=notes,
    )
