"""Grafik dashboard (Altair, sudah ikut terpasang bersama Streamlit).

Aturan warna: warna mengikuti operator (tetap walau filter berubah); warna status hanya untuk
kategori Baik/Cukup/Buruk dan selalu disertai label teks; satu sumbu-y per grafik.
"""

from __future__ import annotations

import altair as alt
import pandas as pd

from .data import CATEGORY_ORDER, METRIC_LABELS, Categories

# Urutan warna seri yang lolos pemeriksaan buta warna untuk seri bersebelahan (biru, oranye, hijau-biru, kuning, ...).
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
STATUS_COLORS = {"Baik": "#0ca30c", "Cukup": "#fab219", "Buruk": "#d03b3b"}
PROBLEM_TINT = "#d03b3b"
MUTED = "#898781"
TIME_FORMAT = "%d/%m %H:%M"


def series_colors(names: list[str]) -> dict[str, str]:
    """Nama seri -> warna, berdasarkan urutan abjad seluruh seri (bukan urutan yang sedang tampil)."""
    ordered = sorted(set(names))
    return {name: SERIES_COLORS[i % len(SERIES_COLORS)] for i, name in enumerate(ordered)}


def _color(field: str, palette: dict[str, str], title: str | None = None, legend: bool = True) -> alt.Color:
    names = list(palette)
    return alt.Color(
        f"{field}:N", title=title, scale=alt.Scale(domain=names, range=[palette[n] for n in names]),
        legend=alt.Legend(orient="top", title=None, symbolType="stroke") if legend else None,
    )


def timeline(points: pd.DataFrame, periods: pd.DataFrame, cats: Categories, color: str,
             metrics: tuple[str, ...] = ("rsrp", "rsrq", "sinr")) -> list[alt.LayerChart]:
    """RSRP/RSRQ/SINR satu operator terhadap waktu (lokal), dengan latar periode diagnosis bermasalah.

    points: kolom ts (waktu lokal tanpa zona) + rsrp, rsrq, sinr. periods: start, end, result (waktu lokal).
    """
    x_domain = None
    if not points.empty:
        x_domain = [points["ts"].min().isoformat(), points["ts"].max().isoformat()]
    span_hours = 0.0 if points.empty else (points["ts"].max() - points["ts"].min()).total_seconds() / 3600
    time_format = "%H:%M" if span_hours <= 24 else TIME_FORMAT
    charts = []
    for i, metric in enumerate(metrics):
        last = i == len(metrics) - 1
        x = alt.X("ts:T", title=None,
                  axis=alt.Axis(format=time_format, labelOverlap=True, tickCount=6, labels=last, ticks=last),
                  scale=alt.Scale(domain=x_domain) if x_domain else alt.Undefined)
        label, unit = METRIC_LABELS[metric]
        poor, good = cats.bounds(metric)
        values = points[metric].dropna()
        lo = min(values.min(), poor) if len(values) else poor
        hi = max(values.max(), good) if len(values) else good
        pad = max(1.0, (hi - lo) * 0.08)
        # minExtent sama di tiap grafik supaya sumbu waktu ketiganya sejajar (grafik dirender terpisah agar
        # mengikuti lebar layar; grafik gabungan vconcat tidak menyesuaikan lebar di HP).
        y = alt.Y(f"{metric}:Q", title=f"{label} ({unit})", axis=alt.Axis(minExtent=44, tickCount=4),
                  scale=alt.Scale(domain=[lo - pad, hi + pad], zero=False))
        layers = []
        if not periods.empty:
            layers.append(
                alt.Chart(periods).mark_rect(color=PROBLEM_TINT, opacity=0.14).encode(
                    x=alt.X("start:T", title=None), x2="end:T",
                    tooltip=[alt.Tooltip("result:N", title="Diagnosis"),
                             alt.Tooltip("start:T", title="Mulai", format=TIME_FORMAT),
                             alt.Tooltip("end:T", title="Selesai", format=TIME_FORMAT)],
                )
            )
        thresholds = pd.DataFrame({"batas": [poor, good], "arti": [f"di bawah ini: Buruk ({poor:g})", f"mulai dari ini: Baik ({good:g})"]})
        layers.append(alt.Chart(thresholds).mark_rule(color=MUTED, strokeDash=[2, 3], strokeWidth=1).encode(
            y="batas:Q", tooltip=[alt.Tooltip("arti:N", title="Batas")]))
        tooltip = [alt.Tooltip("ts:T", title="Waktu", format=TIME_FORMAT),
                   alt.Tooltip(f"{metric}:Q", title=f"{label} ({unit})", format=".1f")]
        base = alt.Chart(points)
        layers.append(base.mark_line(color=color, strokeWidth=2, strokeJoin="round").encode(x=x, y=y))
        layers.append(base.mark_point(color=color, opacity=0, size=220).encode(x=x, y=y, tooltip=tooltip))
        charts.append(alt.layer(*layers).properties(height=150 if last else 130).configure_view(strokeWidth=0))
    return charts


def category_bars(shares: pd.DataFrame, title: str) -> alt.LayerChart:
    """Batang bertumpuk: persentase menit Baik/Cukup/Buruk per kos x operator."""
    df = shares.copy()
    df["baris"] = df["kos"] + " · " + df["operator"]
    df = df.sort_values(["baris", "urutan"], kind="stable")
    df["ujung"] = df.groupby("baris")["persen"].cumsum()
    df["label"] = df["persen"].map(lambda p: f"{p:.0f}%" if p >= 9 else "")
    y = alt.Y("baris:N", title=None, sort=None, axis=alt.Axis(labelLimit=220))
    bars = alt.Chart(df).mark_bar(size=20).encode(
        y=y,
        x=alt.X("persen:Q", title=title, stack="zero", scale=alt.Scale(domain=[0, 100]),
                axis=alt.Axis(values=[0, 25, 50, 75, 100])),
        order=alt.Order("urutan:Q"),
        color=alt.Color("kategori:N", scale=alt.Scale(domain=list(CATEGORY_ORDER),
                                                      range=[STATUS_COLORS[c] for c in CATEGORY_ORDER]),
                        legend=alt.Legend(orient="top", title=None)),
        tooltip=[alt.Tooltip("baris:N", title="Kos · operator"), alt.Tooltip("kategori:N", title="Kategori"),
                 alt.Tooltip("persen:Q", title="Persen waktu", format=".1f"), alt.Tooltip("menit:Q", title="Menit")],
    )
    text = alt.Chart(df).mark_text(dx=-4, align="right", baseline="middle", fontSize=11, fontWeight="bold").encode(
        y=y, x=alt.X("ujung:Q", stack=None), text="label:N",
        color=alt.condition(alt.datum.kategori == "Cukup", alt.value("#0b0b0b"), alt.value("#ffffff")),
    )
    # Tinggi per baris (step), bukan tinggi total: tiap kos x operator mendapat pita sendiri berapa pun jumlahnya.
    return (bars + text).properties(height=alt.Step(38)).configure_view(strokeWidth=0)


def speed_chart(tests: pd.DataFrame, field: str, title: str, palette: dict[str, str],
                good: float | None = None, poor: float | None = None) -> alt.LayerChart:
    """Hasil uji kecepatan terhadap waktu, satu garis per seri (kolom `seri`)."""
    df = tests.dropna(subset=[field])
    shown = {name: palette[name] for name in palette if name in set(df["seri"])}
    x = alt.X("ts:T", title=None, axis=alt.Axis(format=TIME_FORMAT, labelOverlap=True, tickCount=6))
    y = alt.Y(f"{field}:Q", title=title, scale=alt.Scale(zero=True))
    tooltip = [alt.Tooltip("seri:N", title="Operator"), alt.Tooltip("ts:T", title="Waktu", format=TIME_FORMAT),
               alt.Tooltip(f"{field}:Q", title=title, format=".1f"), alt.Tooltip("method:N", title="Jenis uji")]
    base = alt.Chart(df)
    multi = len(shown) > 1
    layers = [
        base.mark_line(strokeWidth=2, strokeJoin="round").encode(x=x, y=y, color=_color("seri", shown, legend=multi)),
        base.mark_point(size=28, filled=True, opacity=1).encode(
            x=x, y=y, color=_color("seri", shown, legend=False), tooltip=tooltip),
    ]
    limits = [(v, t) for v, t in ((poor, "di bawah ini: Buruk"), (good, "mulai dari ini: Baik")) if v is not None]
    if limits:
        rules_df = pd.DataFrame({"batas": [v for v, _ in limits], "arti": [f"{t} ({v:g})" for v, t in limits]})
        layers.insert(0, alt.Chart(rules_df).mark_rule(color=MUTED, strokeDash=[2, 3], strokeWidth=1).encode(
            y="batas:Q", tooltip=[alt.Tooltip("arti:N", title="Batas")]))
    return alt.layer(*layers).properties(height=220).configure_view(strokeWidth=0)
