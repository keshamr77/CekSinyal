"""Pohon keputusan diagnosis untuk SATU jendela (fungsi murni, tanpa database).

Aturan diperiksa berurutan dan berhenti di "ya" pertama (lihat CLAUDE.md). Kelengkapan data sangat
berbeda antar-HP, jadi setiap aturan bisa menyatakan "tidak tersedia": aturan itu dilewati dan
alasannya dicatat di `notes`, alih-alih menebak dari data yang tidak ada.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Iterable, Sequence

LOST = "Sinyal putus"
WEAK = "Cakupan lemah"
INTERFERENCE = "Interferensi"
CONGESTED = "Sel padat"
HANDOVER = "Handover bolak-balik"
NORMAL = "Normal"
INSUFFICIENT = "Data kurang"

# Dari yang terbaik ke yang terburuk (untuk membandingkan operator).
SEVERITY = (NORMAL, HANDOVER, CONGESTED, INTERFERENCE, WEAK, LOST)
PROBLEMS = frozenset(SEVERITY) - {NORMAL}

ADVICE = {
    LOST: "Sinyal hilang di sebagian besar jendela. Kejadian dicatat untuk laporan bukti; coba operator lain.",
    WEAK: "Cakupan lemah: pindah titik (dekat jendela/lantai atas), pakai operator lain, atau ajukan BTS ke operator.",
    INTERFERENCE: "Interferensi: sinyal ada tetapi kotor. Coba operator lain atau kunci band lain.",
    CONGESTED: "Sel padat: sinyal bagus tetapi lambat. Pakai operator lain atau geser jam pemakaian.",
    HANDOVER: "Handover bolak-balik: kunci band di HP atau geser posisi supaya satu sel dominan.",
    NORMAL: "Tidak ada masalah yang terdeteksi.",
    INSUFFICIENT: "Sampel di jendela ini terlalu sedikit untuk didiagnosis.",
}


@dataclass
class Thresholds:
    """Ambang diagnosis; semuanya bisa diubah di config.yaml bagian `diagnosis`."""

    window_minutes: float = 10.0
    min_samples: int = 20             # jendela dengan sampel lebih sedikit -> "Data kurang"
    signal_lost_fraction: float = 0.5  # porsi sampel rat=NONE untuk "Sinyal putus"
    rsrp_weak_dbm: float = -110.0
    sinr_interference_db: float = 0.0
    congestion_ratio: float = 0.5     # median unduh jendela < rasio x median harian
    min_speed_samples: int = 6        # data kecepatan harian minimal untuk aturan sel padat
    speed_lookback_minutes: float = 20.0  # kalau jendela tanpa uji kecepatan, pakai uji terakhir dalam rentang ini
    handover_changes: int = 4
    min_metric_samples: int = 5       # nilai RSRP/SINR minimal supaya median dianggap tersedia


@dataclass
class Sample:
    """Satu baris measurements (raw_ok=1) yang relevan untuk diagnosis, urut waktu."""

    rat: str | None = None
    rsrp: float | None = None
    sinr: float | None = None
    cell_id: int | None = None
    earfcn: int | None = None


@dataclass
class WindowResult:
    result: str
    n_samples: int = 0
    median_rsrp: float | None = None
    median_sinr: float | None = None
    median_download: float | None = None
    cell_changes: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def advice(self) -> str:
        return ADVICE[self.result]

    @property
    def detail(self) -> str:
        return "; ".join(self.notes)


def count_changes(values: Iterable[object]) -> int:
    """Jumlah pergantian nilai berurutan; nilai kosong dilewati (bukan dihitung sebagai pergantian)."""
    changes = 0
    prev = None
    for v in values:
        if v is None:
            continue
        if prev is not None and v != prev:
            changes += 1
        prev = v
    return changes


def _median(values: Sequence[float], minimum: int) -> float | None:
    return float(statistics.median(values)) if len(values) >= max(1, minimum) else None


def evaluate(
    samples: Sequence[Sample],
    window_speeds: Sequence[float] = (),
    daily_speeds: Sequence[float] = (),
    th: Thresholds | None = None,
) -> WindowResult:
    """Diagnosis satu jendela.

    samples: sampel nyata di jendela itu (jumlahnya dihitung apa adanya; data bisa bolong).
    window_speeds: kecepatan unduh (Mbps) yang mewakili jendela ini.
    daily_speeds: kecepatan unduh operator yang sama dalam 24 jam terakhir (pembanding).
    """
    th = th or Thresholds()
    n = len(samples)
    res = WindowResult(result=NORMAL, n_samples=n)
    if n < th.min_samples:
        res.result = INSUFFICIENT
        res.notes.append(f"hanya {n} sampel (minimal {th.min_samples})")
        return res

    served = [s for s in samples if s.rat not in (None, "NONE")]
    lost = n - len(served)

    # Nilai dihitung dulu semuanya supaya tersimpan walau aturan sebelumnya sudah "ya".
    rsrp = [s.rsrp for s in served if s.rsrp is not None]
    res.median_rsrp = _median(rsrp, th.min_metric_samples)

    sinr = [s.sinr for s in served if s.sinr is not None]
    sinr_all_zero = len(sinr) >= th.min_metric_samples and all(v == 0 for v in sinr)
    if not sinr_all_zero:
        res.median_sinr = _median(sinr, th.min_metric_samples)

    if window_speeds:
        res.median_download = float(statistics.median(window_speeds))

    basis = None
    for name in ("cell_id", "earfcn"):
        values = [getattr(s, name) for s in served]
        known = sum(v is not None for v in values)
        if known >= 2:
            basis = name
            res.cell_changes = count_changes(values)
            coverage = f"{known} dari {len(served)} sampel"
            if name == "earfcn":
                res.notes.append(f"handover: dihitung dari earfcn, cell_id tidak tersedia ({coverage})")
            elif known < len(served):
                res.notes.append(f"handover: dihitung dari cell_id ({coverage})")
            if known * 2 < len(served):
                res.notes.append("handover: data identitas sel jarang, jumlah pergantian adalah batas bawah")
            break

    # 1. Sinyal putus
    if lost / n >= th.signal_lost_fraction:
        res.result = LOST
        res.notes.insert(0, f"{lost} dari {n} sampel tanpa layanan")
        return res

    # 2. Cakupan lemah
    if res.median_rsrp is None:
        res.notes.append(f"rsrp: tidak tersedia ({len(rsrp)} nilai)")
    elif res.median_rsrp < th.rsrp_weak_dbm:
        res.result = WEAK
        return res

    # 3. Interferensi
    if sinr_all_zero:
        res.notes.append("sinr: tidak tersedia (selalu 0, kemungkinan nilai kosong dari HP)")
    elif res.median_sinr is None:
        res.notes.append(f"sinr: tidak tersedia ({len(sinr)} nilai)")
    elif res.median_sinr < th.sinr_interference_db:
        res.result = INTERFERENCE
        return res

    # 4. Sel padat
    if not window_speeds:
        res.notes.append("kecepatan: tidak tersedia (tidak ada uji kecepatan untuk jendela ini)")
    elif len(daily_speeds) < th.min_speed_samples:
        res.notes.append(
            f"kecepatan: pembanding harian tidak tersedia ({len(daily_speeds)} data, minimal {th.min_speed_samples})"
        )
    elif res.median_download < th.congestion_ratio * statistics.median(daily_speeds):
        res.result = CONGESTED
        res.notes.append(f"median harian {statistics.median(daily_speeds):.1f} Mbps")
        return res

    # 5. Handover bolak-balik
    if basis is None:
        res.notes.append("handover: tidak tersedia (cell_id dan earfcn kosong)")
    elif res.cell_changes >= th.handover_changes:
        res.result = HANDOVER
        return res

    return res
