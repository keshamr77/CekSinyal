"""Tampilan laporan bukti: Markdown dan HTML satu file (grafik SVG inline, tanpa pustaka luar)."""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta

from ..diagnosis import rules
from .data import METRICS, WEAK_RSRP_DBM, DiagnosisWindow, OperatorReport, ReportData

LANES = (rules.LOST, rules.WEAK, rules.INTERFERENCE, rules.CONGESTED, rules.HANDOVER, rules.NORMAL, rules.INSUFFICIENT)
MAX_TABLE_WINDOWS = 400


# ---------------------------------------------------------------- format umum

def num(value: float | None, digits: int = 1, unit: str = "") -> str:
    if value is None:
        return "–"
    text = f"{value:.{digits}f}".replace("-", "−").replace(".", ",")
    return f"{text} {unit}".strip()


def pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.0f}%".replace(".", ",")


def _t(data: ReportData, dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return "–" if dt is None else dt.astimezone(data.tz).strftime(fmt)


def _duration(hours: float) -> str:
    minutes = round(hours * 60)
    return f"{minutes} menit" if minutes < 60 else f"{minutes // 60} jam {minutes % 60} menit"


def _lost_count(data: ReportData, operator: str) -> int:
    return sum(1 for e in data.lost_events if e.operator == operator)


def _summary_rows(data: ReportData) -> tuple[list[str], list[list[str]]]:
    headers = ["Operator", "Sampel", "Durasi", "Diagnosis dominan", "Median RSRP", f"RSRP < {num(WEAK_RSRP_DBM, 0, 'dBm')}",
               "Median SINR", "Tanpa layanan", "Kejadian putus", "Median unduh"]
    rows = []
    for op in data.operators:
        rsrp, sinr = op.stats.get("rsrp"), op.stats.get("sinr")
        rows.append([
            op.operator, str(op.n_samples), _duration(op.hours), op.dominant or "–",
            num(rsrp.median if rsrp else None, 0, "dBm"), pct(op.pct_weak_rsrp),
            num(sinr.median if sinr else None, 1, "dB"), pct(op.pct_lost),
            str(_lost_count(data, op.operator)), num(op.speed.median_download, 1, "Mbps"),
        ])
    return headers, rows


def _stats_rows(op: OperatorReport) -> tuple[list[str], list[list[str]]]:
    rows = []
    for name, (label, unit, _) in METRICS.items():
        s = op.stats.get(name)
        if s is None:
            rows.append([f"{label} ({unit})", "0", "–", "–", "–", "–", "–"])
        else:
            rows.append([f"{label} ({unit})", str(s.n), num(s.minimum), num(s.p10), num(s.median), num(s.p90), num(s.maximum)])
    return ["Besaran", "n", "Min", "P10", "Median", "P90", "Maks"], rows


def _window_rows(data: ReportData, windows: list[DiagnosisWindow]) -> tuple[list[str], list[list[str]]]:
    headers = [f"Jendela ({data.tz_label})", "Operator", "n", "RSRP", "SINR", "Unduh", "Ganti sel", "Hasil", "Catatan"]
    rows = [[
        f"{_t(data, w.start, '%d/%m %H:%M')}–{_t(data, w.end, '%H:%M')}", w.operator, str(w.n_samples),
        num(w.median_rsrp, 0), num(w.median_sinr, 1), num(w.median_download, 1),
        "–" if w.cell_changes is None else str(w.cell_changes), w.result, w.detail,
    ] for w in windows]
    return headers, rows


def _threshold_rows(data: ReportData) -> list[list[str]]:
    th = data.thresholds
    return [
        ["Jendela diagnosis", f"{th.window_minutes:g} menit, minimal {th.min_samples} sampel"],
        ["Sinyal putus", f"≥ {th.signal_lost_fraction:.0%} sampel tanpa layanan"],
        ["Cakupan lemah", f"median RSRP < {num(th.rsrp_weak_dbm, 0, 'dBm')}"],
        ["Interferensi", f"median SINR < {th.sinr_interference_db:g} dB"],
        ["Sel padat", f"median unduh < {th.congestion_ratio:.0%} median 24 jam (minimal {th.min_speed_samples} uji)"],
        ["Handover bolak-balik", f"≥ {th.handover_changes} pergantian sel per jendela"],
    ]


# ---------------------------------------------------------------- Markdown

def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    def cell(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _md_histogram(bins: list[tuple[float, float, int]], unit: str, width: int = 30) -> str:
    peak = max((c for _, _, c in bins), default=0)
    total = sum(c for _, _, c in bins)
    lines = []
    for lo, hi, count in bins:
        bar = "█" * round(width * count / peak) if peak else ""
        if count and not bar:
            bar = "▏"
        lines.append(f"{lo:>6g} … {hi:<6g} {unit:<3} | {bar:<{width}} {count} ({100 * count / total:.0f}%)")
    return "```\n" + "\n".join(lines) + "\n```"


def render_markdown(data: ReportData) -> str:
    out = [f"# Laporan Bukti Sinyal — {data.kos}", ""]
    out.append(f"- Periode data: {_t(data, data.first)} s.d. {_t(data, data.last)} {data.tz_label}")
    out.append(f"- Dibuat: {_t(data, data.generated)} {data.tz_label} oleh CekSinyal (ET4132, Kelompok 2)")
    out.append("- Alat ini mengukur dan mendiagnosis sinyal; tidak memperkuat sinyal.")
    out.append("")
    for note in data.notes:
        out += [f"> **Catatan:** {note}", ""]
    if not data.operators:
        out.append("Tidak ada data pengukuran untuk kos ini pada periode yang diminta.")
        return "\n".join(out) + "\n"

    out += ["## Ringkasan per operator", "", _md_table(*_summary_rows(data)), ""]
    rec = data.recommendation
    out += [f"**Rekomendasi operator:** {rec.best}. {rec.reason}" if rec.best else f"_{rec.reason}_", ""]

    out += ["## Distribusi sinyal", ""]
    for op in data.operators:
        out += [f"### {op.operator}", ""]
        bands = ", ".join(f"{k} ({v})" for k, v in op.band_counts.items()) or "–"
        out += [f"HP: {', '.join(op.serials) or '–'}. Teknologi/band: {bands}.", "", _md_table(*_stats_rows(op)), ""]
        for name, (label, unit, _) in METRICS.items():
            bins = op.histograms.get(name)
            if bins:
                out += [f"Distribusi {label}:", "", _md_histogram(bins, unit), ""]
        sp = op.speed
        if sp.n_ok or sp.n_failed:
            out += [
                f"Uji kecepatan: {sp.n_ok} berhasil, {sp.n_failed} gagal. Median unduh {num(sp.median_download, 1, 'Mbps')} "
                f"(rentang {num(sp.min_download, 1)}–{num(sp.max_download, 1)}), unggah {num(sp.median_upload, 1, 'Mbps')}, "
                f"ping {num(sp.median_ping, 0, 'ms')}.", "",
            ]

    out += ["## Linimasa diagnosis", ""]
    if data.windows:
        count_rows = [[op.operator] + [str(op.diag_counts.get(name, 0)) for name in LANES] for op in data.operators]
        out += [_md_table(["Operator", *LANES], count_rows), ""]
        shown = data.windows[:MAX_TABLE_WINDOWS]
        out += [_md_table(*_window_rows(data, shown)), ""]
        if len(data.windows) > len(shown):
            out += [f"_{len(data.windows) - len(shown)} jendela berikutnya tidak ditampilkan; lihat tabel `diagnoses`._", ""]
    else:
        out += ["Belum ada diagnosis.", ""]

    out += ["## Kejadian sinyal putus", ""]
    if data.lost_events or data.lost_windows:
        out += [f"{len(data.lost_events)} kejadian putus, {len(data.lost_windows)} jendela berstatus \"Sinyal putus\".", ""]
        if data.lost_events:
            out += [_md_table([f"Waktu ({data.tz_label})", "Operator", "Keterangan"],
                              [[_t(data, e.ts), e.operator or "–", e.detail] for e in data.lost_events]), ""]
    else:
        out += ["Tidak ada kejadian sinyal putus pada periode ini.", ""]

    if data.collector_events:
        out += ["## Gangguan pengumpulan data", "",
                "Kejadian ini berasal dari alat ukur (HP terputus, ADB, parsing), bukan dari jaringan operator.", "",
                _md_table([f"Waktu ({data.tz_label})", "Operator", "Keterangan"],
                          [[_t(data, e.ts), e.operator or "–", e.detail] for e in data.collector_events[:50]]), ""]

    out += ["## Keterbatasan data per HP", ""]
    for op in data.operators:
        out.append(f"**{op.operator}** ({', '.join(op.serials) or '–'}):")
        out += [f"- {note}" for note in op.limitations] or ["- Tidak ada keterbatasan yang terdeteksi."]
        out.append("")

    out += ["## Ambang yang dipakai", "", _md_table(["Aturan", "Syarat"], _threshold_rows(data)), ""]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- SVG

def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _nice_ceil(value: float) -> int:
    if value <= 5:
        return max(1, int(value + 0.999))
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * magnitude:
            return int(step * magnitude)
    return int(10 * magnitude)


def svg_histogram(bins: list[tuple[float, float, int]], label: str, unit: str, threshold: float | None = None) -> str:
    w, h, left, right, top, bottom = 340, 190, 34, 8, 16, 30
    plot_w, plot_h = w - left - right, h - top - bottom
    peak = _nice_ceil(max(c for _, _, c in bins))
    slot = plot_w / len(bins)
    bar_w = max(2.0, min(24.0, slot - 2))
    base = top + plot_h
    total = sum(c for _, _, c in bins)
    parts = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Distribusi {_e(label)}">']
    for frac in (0, 0.5, 1):
        y = base - plot_h * frac
        cls = "axis" if frac == 0 else "grid"
        parts.append(f'<line class="{cls}" x1="{left}" x2="{w - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 5}" y="{y + 3.5:.1f}" text-anchor="end">{peak * frac:g}</text>')
    every = max(1, -(-len(bins) // 7))
    for i, (lo, hi, count) in enumerate(bins):
        x = left + i * slot + (slot - bar_w) / 2
        if i % every == 0:
            parts.append(f'<text class="tick" x="{left + i * slot:.1f}" y="{base + 14}" text-anchor="middle">{lo:g}</text>')
        if not count:
            continue
        bar_h = max(1.5, plot_h * count / peak)
        r = min(4.0, bar_h, bar_w / 2)
        y = base - bar_h
        share = 100 * count / total
        parts.append(
            f'<path class="bar" d="M{x:.1f},{base} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
            f'H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{base} Z">'
            f'<title>{lo:g} sampai {hi:g} {_e(unit)}: {count} sampel ({share:.0f}%)</title></path>'
        )
    if threshold is not None and bins[0][0] <= threshold <= bins[-1][1]:
        x = left + (threshold - bins[0][0]) / (bins[-1][1] - bins[0][0]) * plot_w
        anchor = "start" if x < left + plot_w / 2 else "end"
        dx = 4 if anchor == "start" else -4
        parts.append(f'<line class="threshold" x1="{x:.1f}" x2="{x:.1f}" y1="{top - 6}" y2="{base}"/>')
        parts.append(f'<text class="tick" x="{x + dx:.1f}" y="{top - 2}" text-anchor="{anchor}">ambang {threshold:g}</text>')
    parts.append(f'<text class="tick" x="{w - right}" y="{h - 2}" text-anchor="end">{_e(unit)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _time_ticks(t0: datetime, t1: datetime, tz, max_ticks: int = 8) -> tuple[list[datetime], str]:
    span = max((t1 - t0).total_seconds(), 60)
    for step in (300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800):
        if span / step <= max_ticks:
            break
    offset = tz.utcoffset(None).total_seconds()
    first = (int(t0.timestamp() + offset) // step + 1) * step - offset
    ticks = []
    t = first
    while t <= t1.timestamp():
        ticks.append(datetime.fromtimestamp(t, tz=tz))
        t += step
    fmt = "%H:%M" if span <= 86400 else "%d/%m %H:%M" if step < 86400 else "%d/%m"
    return ticks, fmt


class _TimeScale:
    def __init__(self, t0: datetime, t1: datetime, left: float, width: float) -> None:
        if t1 <= t0:
            t1 = t0 + timedelta(minutes=10)
        self.t0, self.t1, self.left, self.width = t0, t1, left, width

    def __call__(self, t: datetime) -> float:
        return self.left + (t - self.t0) / (self.t1 - self.t0) * self.width


def _time_axis(scale: _TimeScale, tz, y: float, top: float | None = None) -> list[str]:
    ticks, fmt = _time_ticks(scale.t0, scale.t1, tz)
    parts = []
    for tick in ticks:
        x = scale(tick)
        if top is not None:
            parts.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{y}"/>')
        parts.append(f'<text class="tick" x="{x:.1f}" y="{y + 14}" text-anchor="middle">{tick.strftime(fmt)}</text>')
    return parts


def svg_rsrp_lines(data: ReportData, weak: float) -> str:
    """RSRP per waktu, satu garis per operator (warna mengikuti urutan operator, tetap)."""
    ops = [op for op in data.operators if any(v is not None for _, v in op.series)][:8]
    if not ops or data.first is None or data.last is None:
        return ""
    w, h, left, right, top, bottom = 760, 250, 44, 12, 12, 28
    plot_h = h - top - bottom
    values = [v for op in ops for _, v in op.series if v is not None]
    lo = min(min(values), weak) // 10 * 10
    hi = -(-max(values) // 10) * 10
    if hi == lo:
        hi += 10
    scale = _TimeScale(data.first, data.last, left, w - left - right)

    def y_of(v: float) -> float:
        return top + (hi - v) / (hi - lo) * plot_h

    parts = [f'<svg class="chart linechart" viewBox="0 0 {w} {h}" role="img" aria-label="RSRP per waktu">']
    steps = int((hi - lo) // 10)
    every = max(1, -(-steps // 6))
    for i in range(0, steps + 1, every):
        v = lo + 10 * i
        parts.append(f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y_of(v):.1f}" y2="{y_of(v):.1f}"/>')
        label = f"{v:g}".replace("-", "−")
        parts.append(f'<text class="tick" x="{left - 6}" y="{y_of(v) + 3.5:.1f}" text-anchor="end">{label}</text>')
    parts += _time_axis(scale, data.tz, top + plot_h)
    parts.append(f'<line class="threshold" x1="{left}" x2="{w - right}" y1="{y_of(weak):.1f}" y2="{y_of(weak):.1f}"/>')
    parts.append(f'<text class="tick" x="{w - right}" y="{y_of(weak) - 4:.1f}" text-anchor="end">ambang cakupan lemah {weak:g} dBm</text>')

    payload = []
    for idx, op in enumerate(ops, start=1):
        segments, current, points = [], [], []
        for ts, value in op.series:
            if value is None:
                if current:
                    segments.append(current)
                current = []
                continue
            x, y = scale(ts), y_of(value)
            current.append(f"{x:.1f},{y:.1f}")
            points.append([round(x, 1), round(y, 1), value, ts.astimezone(data.tz).strftime("%d/%m %H:%M:%S")])
        if current:
            segments.append(current)
        for seg in segments:
            if len(seg) == 1:
                x, y = seg[0].split(",")
                parts.append(f'<circle class="dot s{idx}" cx="{x}" cy="{y}" r="2"/>')
            else:
                parts.append(f'<polyline class="line l{idx}" points="{" ".join(seg)}"/>')
        payload.append({"name": op.operator, "slot": idx, "points": points})
    parts.append(f'<line class="crosshair" x1="0" x2="0" y1="{top}" y2="{top + plot_h}" visibility="hidden"/>')
    parts.append(f'<rect class="hit" x="{left}" y="{top}" width="{w - left - right}" height="{plot_h}"/>')
    parts.append("</svg>")
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return "".join(parts) + f'<script type="application/json" class="linedata">{blob}</script>'


def svg_lanes(data: ReportData, windows: list[DiagnosisWindow], t0: datetime, t1: datetime) -> str:
    """Linimasa diagnosis satu operator: posisi baris = jenis diagnosis (tidak bergantung pada warna)."""
    w, left, right, top, row_h = 760, 150, 12, 6, 20
    plot_h = row_h * len(LANES)
    h = top + plot_h + 28
    scale = _TimeScale(t0, t1, left, w - left - right)
    parts = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" aria-label="Linimasa diagnosis">']
    counts = {name: sum(1 for x in windows if x.result == name) for name in LANES}
    for i, name in enumerate(LANES):
        y = top + i * row_h
        parts.append(f'<line class="grid" x1="{left}" x2="{w - right}" y1="{y + row_h}" y2="{y + row_h}"/>')
        parts.append(f'<text class="lane" x="{left - 8}" y="{y + 14}" text-anchor="end">{_e(name)} ({counts[name]})</text>')
    parts += _time_axis(scale, data.tz, top + plot_h, top)
    for win in windows:
        if win.result not in LANES:
            continue
        x0, x1 = scale(win.start), scale(win.end)
        width = max(1.5, x1 - x0 - (2 if x1 - x0 > 5 else 0))
        y = top + LANES.index(win.result) * row_h + 4
        cls = "lane-ok" if win.result == rules.NORMAL else "lane-na" if win.result == rules.INSUFFICIENT else "lane-problem"
        tip = (f"{_t(data, win.start, '%d/%m %H:%M')}–{_t(data, win.end, '%H:%M')} {data.tz_label}: {win.result}, "
               f"{win.n_samples} sampel, RSRP {num(win.median_rsrp, 0, 'dBm')}, SINR {num(win.median_sinr, 1, 'dB')}")
        parts.append(f'<rect class="{cls}" x="{x0:.1f}" y="{y}" width="{width:.1f}" height="12" rx="2"><title>{_e(tip)}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- HTML

CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--deemph:#c3c2b7;--na:#e1e0d9;
--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--series-4:#eda100;--series-5:#e87ba4;--series-6:#008300;
--series-7:#6250d6;--series-8:#e34948}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;
--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--deemph:#52514e;
--na:#383835;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--series-4:#c98500;--series-5:#d55181;
--series-6:#008300;--series-7:#9085e9;--series-8:#e66767}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:36px 0 12px}h3{font-size:16px;margin:24px 0 8px}
p{margin:8px 0}.meta{color:var(--ink2)}.muted{color:var(--muted)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin:12px 0}
.note{border-left:3px solid var(--axis)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:10px 0}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.tile .label{color:var(--ink2);font-size:12.5px}.tile .value{font-size:22px;font-weight:600;line-height:1.3}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--ink2);font-weight:600;white-space:nowrap}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}
.charts figure,.wide{margin:0;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 12px;position:relative}
figcaption{font-weight:600;font-size:13.5px;margin-bottom:4px}
.chart{display:block;width:100%;height:auto}
.chart text{font:11px system-ui,-apple-system,"Segoe UI",sans-serif;fill:var(--muted);font-variant-numeric:tabular-nums}
.chart text.lane{fill:var(--ink2);font-size:12px}
.grid{stroke:var(--grid);stroke-width:1}.axis{stroke:var(--axis);stroke-width:1}
.threshold{stroke:var(--ink2);stroke-width:1}.crosshair{stroke:var(--axis);stroke-width:1}
.bar{fill:var(--series-1)}.bar:hover,.lane-problem:hover,.lane-ok:hover,.lane-na:hover{opacity:.7}
.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.l1{stroke:var(--series-1)}.l2{stroke:var(--series-2)}.l3{stroke:var(--series-3)}.l4{stroke:var(--series-4)}
.l5{stroke:var(--series-5)}.l6{stroke:var(--series-6)}.l7{stroke:var(--series-7)}.l8{stroke:var(--series-8)}
.s1{fill:var(--series-1)}.s2{fill:var(--series-2)}.s3{fill:var(--series-3)}.s4{fill:var(--series-4)}
.s5{fill:var(--series-5)}.s6{fill:var(--series-6)}.s7{fill:var(--series-7)}.s8{fill:var(--series-8)}
.k1{background:var(--series-1)}.k2{background:var(--series-2)}.k3{background:var(--series-3)}.k4{background:var(--series-4)}
.k5{background:var(--series-5)}.k6{background:var(--series-6)}.k7{background:var(--series-7)}.k8{background:var(--series-8)}
.hit{fill:transparent}.lane-problem{fill:var(--series-1)}.lane-ok{fill:var(--deemph)}.lane-na{fill:var(--na)}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:13px;color:var(--ink2);margin:2px 0 6px}
.key{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}
.swatch{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:-1px;margin-right:6px}
.tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;
padding:6px 10px;font-size:12.5px;box-shadow:0 4px 14px rgba(0,0,0,.12);white-space:nowrap;z-index:2}
.tip .when{color:var(--ink2);margin-bottom:2px}.tip b{font-weight:600}
ul{margin:6px 0;padding-left:20px}details summary{cursor:pointer;color:var(--ink2);margin:8px 0}
code{font-size:.92em}
@media print{body{background:#fff}.card,.tile,.charts figure,.wide{break-inside:avoid}}
"""

JS = """
document.querySelectorAll('.linechart').forEach(function (svg) {
  var holder = svg.parentNode, dataEl = holder.querySelector('.linedata');
  if (!dataEl) return;
  var series = JSON.parse(dataEl.textContent), hit = svg.querySelector('.hit'), cross = svg.querySelector('.crosshair');
  var tip = document.createElement('div'); tip.className = 'tip'; tip.hidden = true; holder.appendChild(tip);
  function nearest(points, x) {
    var best = null, d = Infinity;
    for (var i = 0; i < points.length; i++) { var dd = Math.abs(points[i][0] - x); if (dd < d) { d = dd; best = points[i]; } }
    return d <= 12 ? best : null;
  }
  function move(ev) {
    var box = svg.getBoundingClientRect(), vb = svg.viewBox.baseVal;
    var x = (ev.clientX - box.left) / box.width * vb.width, anchor = null, rows = [];
    series.forEach(function (s) {
      var p = nearest(s.points, x);
      if (p && (!anchor || Math.abs(p[0] - x) < Math.abs(anchor[0] - x))) anchor = p;
      rows.push([s, p]);
    });
    if (!anchor) { leave(); return; }
    cross.setAttribute('x1', anchor[0]); cross.setAttribute('x2', anchor[0]); cross.setAttribute('visibility', 'visible');
    tip.textContent = '';
    var when = document.createElement('div'); when.className = 'when'; when.textContent = anchor[3]; tip.appendChild(when);
    rows.forEach(function (r) {
      var row = document.createElement('div'), key = document.createElement('span'), val = document.createElement('b');
      key.className = 'key k' + r[0].slot; row.appendChild(key);
      val.textContent = r[1] ? String(r[1][2]).replace('-', '\\u2212') + ' dBm' : '\\u2013'; row.appendChild(val);
      row.appendChild(document.createTextNode(' ' + r[0].name)); tip.appendChild(row);
    });
    tip.hidden = false;
    var hb = holder.getBoundingClientRect(), left = ev.clientX - hb.left + 14;
    if (left + tip.offsetWidth > hb.width) left = ev.clientX - hb.left - tip.offsetWidth - 14;
    tip.style.left = Math.max(0, left) + 'px'; tip.style.top = (ev.clientY - hb.top + 12) + 'px';
  }
  function leave() { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); }
  hit.addEventListener('pointermove', move); hit.addEventListener('pointerleave', leave);
});
"""


def _html_table(headers: list[str], rows: list[list[str]], numeric: set[int] = frozenset()) -> str:
    def cells(tag: str, row: list[str]) -> str:
        return "".join(
            f"<{tag}{' class=n' if i in numeric else ''}>{_e(c)}</{tag}>" for i, c in enumerate(row)
        )

    body = "".join(f"<tr>{cells('td', r)}</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{cells("th", headers)}</tr></thead><tbody>{body}</tbody></table></div>'


def _tile(label: str, value: str) -> str:
    return f'<div class="tile"><div class="label">{_e(label)}</div><div class="value">{_e(value)}</div></div>'


def render_html(data: ReportData) -> str:
    th = data.thresholds
    out = [
        '<!doctype html><html lang="id"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Laporan Bukti Sinyal — {_e(data.kos)}</title><style>{CSS}</style></head><body><main>",
        f"<h1>Laporan Bukti Sinyal — {_e(data.kos)}</h1>",
        f'<p class="meta">Periode data {_e(_t(data, data.first))} s.d. {_e(_t(data, data.last))} {_e(data.tz_label)}. '
        f"Dibuat {_e(_t(data, data.generated))} {_e(data.tz_label)} oleh CekSinyal (ET4132, Kelompok 2). "
        "Alat ini mengukur dan mendiagnosis sinyal; tidak memperkuat sinyal.</p>",
    ]
    for note in data.notes:
        out.append(f'<div class="card note"><b>Catatan:</b> {_e(note)}</div>')
    if not data.operators:
        out.append("<p>Tidak ada data pengukuran untuk kos ini pada periode yang diminta.</p></main></body></html>")
        return "".join(out)

    out.append("<h2>Ringkasan per operator</h2>")
    for op in data.operators:
        rsrp, sinr = op.stats.get("rsrp"), op.stats.get("sinr")
        out.append(f"<h3>{_e(op.operator)}</h3><div class=\"tiles\">")
        out.append(_tile("Diagnosis dominan", op.dominant or "–"))
        out.append(_tile("Median RSRP", num(rsrp.median if rsrp else None, 0, "dBm")))
        out.append(_tile(f"Waktu RSRP < {WEAK_RSRP_DBM:g} dBm".replace("-", "−"), pct(op.pct_weak_rsrp)))
        out.append(_tile("Median SINR", num(sinr.median if sinr else None, 1, "dB")))
        out.append(_tile("Kejadian putus", str(_lost_count(data, op.operator))))
        out.append(_tile("Median unduh", num(op.speed.median_download, 1, "Mbps")))
        out.append("</div>")
    headers, rows = _summary_rows(data)
    out.append(_html_table(headers, rows, numeric={1, 4, 5, 6, 7, 8, 9}))
    rec = data.recommendation
    if rec.best:
        out.append(f'<div class="card"><b>Rekomendasi operator: {_e(rec.best)}.</b> {_e(rec.reason)}</div>')
    else:
        out.append(f'<p class="muted">{_e(rec.reason)}</p>')

    lines = svg_rsrp_lines(data, th.rsrp_weak_dbm)
    if lines:
        out.append("<h2>RSRP sepanjang waktu</h2><div class=\"wide\">")
        shown = [op for op in data.operators if any(v is not None for _, v in op.series)][:8]
        if len(shown) > 1:
            out.append('<div class="legend">' + "".join(
                f'<span><span class="key k{i}"></span>{_e(op.operator)}</span>' for i, op in enumerate(shown, start=1)
            ) + "</div>")
        else:
            out.append(f"<figcaption>{_e(shown[0].operator)}, dBm</figcaption>")
        out.append(lines + "</div>")
        out.append('<p class="muted">Garis terputus berarti tidak ada sampel pada rentang itu (collector berhenti atau HP terlepas).</p>')

    out.append("<h2>Distribusi sinyal</h2>")
    thresholds = {"rsrp": th.rsrp_weak_dbm, "sinr": th.sinr_interference_db}
    for op in data.operators:
        bands = ", ".join(f"{k} ({v})" for k, v in op.band_counts.items()) or "–"
        out.append(f"<h3>{_e(op.operator)}</h3>")
        out.append(f'<p class="meta">HP: {_e(", ".join(op.serials) or "–")}. Teknologi/band: {_e(bands)}.</p>')
        out.append('<div class="charts">')
        for name, (label, unit, _) in METRICS.items():
            bins = op.histograms.get(name)
            if bins:
                out.append(f"<figure><figcaption>{_e(label)}, jumlah sampel per rentang</figcaption>"
                           f"{svg_histogram(bins, label, unit, thresholds.get(name))}</figure>")
        out.append("</div>")
        headers, rows = _stats_rows(op)
        out.append(_html_table(headers, rows, numeric={1, 2, 3, 4, 5, 6}))
        sp = op.speed
        if sp.n_ok or sp.n_failed:
            out.append(
                f"<p>Uji kecepatan: {sp.n_ok} berhasil, {sp.n_failed} gagal. Median unduh "
                f"{_e(num(sp.median_download, 1, 'Mbps'))} (rentang {_e(num(sp.min_download, 1))}–{_e(num(sp.max_download, 1))}), "
                f"unggah {_e(num(sp.median_upload, 1, 'Mbps'))}, ping {_e(num(sp.median_ping, 0, 'ms'))}.</p>"
            )

    out.append("<h2>Linimasa diagnosis</h2>")
    if data.windows:
        t0 = min(w.start for w in data.windows)
        t1 = max(w.end for w in data.windows)
        out.append('<div class="legend"><span><span class="swatch" style="background:var(--series-1)"></span>bermasalah</span>'
                   '<span><span class="swatch" style="background:var(--deemph)"></span>normal</span>'
                   '<span><span class="swatch" style="background:var(--na)"></span>data kurang</span></div>')
        for op in data.operators:
            wins = [w for w in data.windows if w.operator == op.operator]
            if wins:
                out.append(f'<div class="wide" style="margin-bottom:10px"><figcaption>{_e(op.operator)}: '
                           f"{len(wins)} jendela {th.window_minutes:g} menit</figcaption>{svg_lanes(data, wins, t0, t1)}</div>")
        headers, rows = _window_rows(data, data.windows[:MAX_TABLE_WINDOWS])
        table = _html_table(headers, rows, numeric={2, 3, 4, 5, 6})
        extra = len(data.windows) - MAX_TABLE_WINDOWS
        if extra > 0:
            table += f'<p class="muted">{extra} jendela berikutnya tidak ditampilkan; lihat tabel diagnoses.</p>'
        out.append(f"<details{' open' if len(rows) <= 30 else ''}><summary>Tabel semua jendela ({len(data.windows)})</summary>{table}</details>")
    else:
        out.append("<p>Belum ada diagnosis.</p>")

    out.append("<h2>Kejadian sinyal putus</h2>")
    if data.lost_events or data.lost_windows:
        out.append(f"<p>{len(data.lost_events)} kejadian putus, {len(data.lost_windows)} jendela berstatus \"Sinyal putus\".</p>")
        if data.lost_events:
            out.append(_html_table([f"Waktu ({data.tz_label})", "Operator", "Keterangan"],
                                   [[_t(data, e.ts), e.operator or "–", e.detail] for e in data.lost_events]))
    else:
        out.append("<p>Tidak ada kejadian sinyal putus pada periode ini.</p>")

    if data.collector_events:
        out.append("<h2>Gangguan pengumpulan data</h2><p class=\"meta\">Kejadian ini berasal dari alat ukur (HP terputus, ADB, "
                   "parsing), bukan dari jaringan operator.</p>")
        out.append(_html_table([f"Waktu ({data.tz_label})", "Operator", "Keterangan"],
                               [[_t(data, e.ts), e.operator or "–", e.detail] for e in data.collector_events[:50]]))

    out.append("<h2>Keterbatasan data per HP</h2>")
    for op in data.operators:
        items = "".join(f"<li>{_e(n)}</li>" for n in op.limitations) or "<li>Tidak ada keterbatasan yang terdeteksi.</li>"
        out.append(f'<div class="card"><b>{_e(op.operator)}</b> <span class="muted">({_e(", ".join(op.serials) or "–")})</span><ul>{items}</ul></div>')

    out.append("<h2>Ambang yang dipakai</h2>")
    out.append(_html_table(["Aturan", "Syarat"], _threshold_rows(data)))
    out.append(f"</main><script>{JS}</script></body></html>")
    return "".join(out)
