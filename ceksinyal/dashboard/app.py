"""Aplikasi Streamlit CekSinyal. Dijalankan lewat dashboard/streamlit_app.py
(`python -m ceksinyal dashboard` atau Streamlit Community Cloud).

Semua perhitungan ada di data.py; file ini hanya menyusun tampilan.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from .. import db
from ..diagnosis import rules
from . import charts, data, settings as settings_mod
from .data import GOOD, FAIR, POOR, NO_DATA, Card, Categories, Dataset

VIEW_HOME, VIEW_DETAIL = "Beranda", "Detail lebih lanjut"
VIEWS = [VIEW_HOME, VIEW_DETAIL]
TABS = ["Ringkasan teknis", "Linimasa", "Perbandingan", "Kecepatan", "Kejadian", "Kualitas data", "Cara membaca"]
CATEGORY_BADGE = {GOOD: ":green-badge[✅ Baik]", FAIR: ":orange-badge[⚠️ Cukup]", POOR: ":red-badge[⛔ Buruk]",
                  NO_DATA: ":gray-badge[– Tidak ada data]"}
RANGES = {"24 jam terakhir": 24, "3 hari terakhir": 72, "7 hari terakhir": 168, "Semua data": None}
METRIC_HINT = {"rsrp": "kekuatan sinyal", "rsrq": "kualitas sinyal", "sinr": "kebersihan sinyal"}
DIAGNOSIS_PLAIN = {
    rules.NORMAL: "Tidak ada masalah yang terdeteksi.",
    rules.LOST: "HP kehilangan layanan di sebagian besar waktu pada periode itu.",
    rules.WEAK: "Sinyal dari menara terlalu lemah sampai di titik ini (jarak, tembok, atau lantai).",
    rules.INTERFERENCE: "Sinyalnya ada, tetapi \"kotor\" karena tertabrak sinyal lain, sehingga internet tetap lambat.",
    rules.CONGESTED: "Sinyal bagus tetapi menaranya sedang ramai dipakai, jadi kecepatan turun jauh di bawah biasanya.",
    rules.HANDOVER: "HP bolak-balik pindah menara, sehingga koneksi sering tersendat.",
    rules.INSUFFICIENT: "Datanya terlalu sedikit untuk disimpulkan (alat ukur baru menyala atau sempat berhenti).",
}


# Bar seperti indikator sinyal HP: makin banyak bar terisi, makin bagus. Warna hanya penegas; level juga
# selalu ditulis dengan ikon + teks.
BARS = ("▂", "▃", "▅", "▆", "█")
LEVEL_COLOR = {5: "green", 4: "green", 3: "orange", 2: "red", 1: "red"}
LEVEL_ICON = {5: "✅", 4: "✅", 3: "⚠️", 2: "⛔", 1: "⛔", 0: "📵"}


def level_bars(level: int | None) -> str:
    """Lima bar Markdown berwarna, mis. level 3 -> ':orange[▂▃▅]:gray[▆█]'."""
    if not level:
        return f":gray[{''.join(BARS)}]"
    filled, empty = "".join(BARS[:level]), "".join(BARS[level:])
    return f":{LEVEL_COLOR[level]}[{filled}]" + (f":gray[{empty}]" if empty else "")


def level_title(lv: data.Level) -> str:
    icon = LEVEL_ICON.get(lv.level, "❔") if lv.level is not None else "❔"
    rank = f" · {lv.level} dari 5" if lv.level else ""
    return f"{icon} {lv.name}{rank}"


def diagnosis_icon(result: str | None) -> str:
    if result == rules.NORMAL:
        return "✅"
    if result == rules.LOST:
        return "⛔"
    if result in rules.PROBLEMS:
        return "⚠️"
    return "❔"


def num(value: float | None, digits: int = 0, unit: str = "") -> str:
    if value is None or pd.isna(value):
        return "–"
    return (f"{value:.{digits}f}".replace("-", "−").replace(".", ",") + f" {unit}").strip()


def _stretch(func, *args, **kwargs):
    """Lebar penuh di versi Streamlit baru (width="stretch") maupun lama (use_container_width)."""
    try:
        return func(*args, width="stretch", **kwargs)
    except TypeError:
        return func(*args, use_container_width=True, **kwargs)


def _table(df: pd.DataFrame, **kwargs) -> None:
    _stretch(st.dataframe, df, hide_index=True, **kwargs)


class Context:
    """Semua yang dibutuhkan halaman: data yang sudah disaring dan dialiaskan, ambang, zona waktu."""

    def __init__(self, full: Dataset, view: Dataset, s: settings_mod.DashboardSettings, now: datetime) -> None:
        self.full, self.view, self.settings, self.now = full, view, s, now
        self.cfg = s.config
        self.cats = Categories.from_config(s.config)
        self.tz = s.config.tz
        self.tz_label = s.config.tz_label
        self.colors = charts.series_colors(list(full.measurements["operator"].dropna().unique()))

    def local(self, series: pd.Series) -> pd.Series:
        return data.to_local(series, self.tz)

    def fmt(self, dt, fmt: str = "%d/%m %H:%M") -> str:
        return data.fmt_local(dt, self.tz, fmt)


# ---------------------------------------------------------------- pemuatan

def _secrets() -> dict:
    try:
        return {key: st.secrets[key] for key in st.secrets}
    except Exception:  # tidak ada secrets.toml (jalan lokal): bukan kesalahan
        return {}


def _load(s: settings_mod.DashboardSettings) -> Dataset:
    ttl = max(0, int(s.config.dashboard.cache_ttl_seconds))

    @st.cache_data(ttl=ttl or None, show_spinner="Memuat data…")
    def cached(key: tuple, _settings: settings_mod.DashboardSettings) -> Dataset:
        return settings_mod.load_dataset(_settings)

    return cached(s.cache_key, s)


# ---------------------------------------------------------------- halaman

def _cards(ctx: Context) -> list[Card]:
    d = ctx.cfg.dashboard
    return data.summary_cards(ctx.view, ctx.cats, ctx.now, d.stale_minutes, d.level_window_minutes)


def page_home(ctx: Context) -> None:
    """Beranda untuk orang awam: satu kartu per operator dengan level 1-5 seperti bar sinyal HP."""
    cards = _cards(ctx)
    if not cards:
        st.info("Tidak ada data untuk pilihan ini.")
        return
    st.caption(f"Kualitas internet seluler di kos, dinilai 1 sampai 5 seperti bar sinyal di HP, dari "
               f"{num(ctx.cfg.dashboard.level_window_minutes)} menit pengukuran terakhir.")
    recs = {r.kos: r for r in data.recommendations(ctx.view)}
    for kos in sorted({c.kos for c in cards}):
        st.subheader(kos, anchor=False)
        group = sorted((c for c in cards if c.kos == kos), key=lambda c: c.operator)
        rec = recs.get(kos)
        if rec and rec.best and len(group) > 1:
            st.success(f"Dari pengukuran selama ini, {rec.best} paling jarang bermasalah di kos ini.",
                       title=f"Paling bagus di sini: {rec.best}", icon="👍")
        columns = st.columns(min(3, len(group)))
        for i, card in enumerate(group):
            with columns[i % len(columns)]:
                _level_card(ctx, card)
    with st.expander("Arti level 1 sampai 5"):
        _table(pd.DataFrame([{"Level": f"{n}  {''.join(BARS[:n])}",
                              "Kualitas": f"{LEVEL_ICON[n]} {data.LEVEL_NAMES[n]}", "Cocok untuk": data.LEVEL_USES[n]}
                             for n in (5, 4, 3, 2, 1)]))
        st.caption("Level dihitung dari kekuatan sinyal, kebersihan sinyal, dan kecepatan internet terbaru; yang "
                   "paling lemah menentukan level, karena koneksi hanya sebaik bagian terlemahnya. Angka teknisnya ada "
                   f"di {VIEW_DETAIL}.")


def _level_card(ctx: Context, card: Card) -> None:
    lv = card.level or data.Level(None, {})
    with st.container(border=True):
        st.markdown(f"#### {card.operator}")
        st.header(level_bars(lv.level), anchor=False)
        st.markdown(f"**{level_title(lv)}**")
        st.markdown(lv.uses)
        if lv.cause:
            st.markdown(f"**Penyebab utama:** {lv.cause}")
        if card.stale:
            st.warning(f"Alat ukur tidak mengirim data sejak {data.format_ago(card.minutes_ago)}. "
                       "Nilai di atas adalah kondisi terakhir yang terukur.", icon="⏸️")
        else:
            st.caption(f"Diperbarui {data.format_ago(card.minutes_ago)} ({ctx.fmt(card.last_seen, '%H:%M')} {ctx.tz_label})")


def page_summary(ctx: Context) -> None:
    cards = _cards(ctx)
    if not cards:
        st.info("Tidak ada data untuk pilihan ini.")
        return
    st.caption("Satu kartu per kos dan operator. Angka sinyal adalah nilai tengah (median) dari 24 jam sebelum data "
               "terakhir masing-masing.")
    for kos in sorted({c.kos for c in cards}):
        st.subheader(kos)
        group = [c for c in cards if c.kos == kos]
        columns = st.columns(min(3, len(group)))
        for i, card in enumerate(group):
            with columns[i % len(columns)]:
                _card(ctx, card)


def _card(ctx: Context, card: Card) -> None:
    with st.container(border=True):
        st.markdown(f"#### {card.operator}")
        if card.stale:
            st.warning(f"Alat ukur diam: data terakhir {data.format_ago(card.minutes_ago)} "
                       f"({ctx.fmt(card.last_seen)} {ctx.tz_label}). Angka di bawah mungkin sudah tidak berlaku.",
                       icon="⏸️")
        else:
            st.caption(f"Terakhir terlihat {data.format_ago(card.minutes_ago)} ({ctx.fmt(card.last_seen, '%H:%M')} {ctx.tz_label})")
        if card.diagnosis:
            st.markdown(f"**{diagnosis_icon(card.diagnosis)} {card.diagnosis}**  \n"
                        f"{card.advice or DIAGNOSIS_PLAIN.get(card.diagnosis, '')}")
            st.caption(f"Diagnosis terakhir, jendela sampai {ctx.fmt(card.diagnosis_end)} {ctx.tz_label}")
        else:
            st.markdown("**❔ Belum ada diagnosis**")
        for name in ("rsrp", "rsrq", "sinr"):
            label, unit = data.METRIC_LABELS[name]
            digits = 1 if name == "sinr" else 0
            st.markdown(f"**{label}** ({METRIC_HINT[name]}): {num(card.medians[name], digits, unit)} "
                        f"{CATEGORY_BADGE[card.categories[name]]}")
        when = f" · {ctx.fmt(card.download_ts)}" if card.download_ts else ""
        st.markdown(f"**Unduh terakhir**: {num(card.download, 1, 'Mbps')} {CATEGORY_BADGE[card.download_category]}{when}")
        if card.pct_no_service:
            st.caption(f"Tanpa layanan {num(card.pct_no_service, 1)}% dari waktu terukur.")


def page_timeline(ctx: Context) -> None:
    m = ctx.view.measurements
    if m.empty:
        st.info("Tidak ada data untuk pilihan ini.")
        return
    kos_names = ctx.view.kos_names()
    kos = st.selectbox("Kos", kos_names, key="linimasa_kos") if len(kos_names) > 1 else kos_names[0]
    st.caption(f"Waktu dalam {ctx.tz_label}. Latar merah muda = periode yang didiagnosis bermasalah (arahkan kursor "
               "atau lihat tabel di bawah grafik). Garis putus-putus = batas kategori. Garis terputus = alat ukur tidak merekam.")
    points = data.downsample(m[m["kos"] == kos])
    periods = data.problem_periods(ctx.view.diagnoses[ctx.view.diagnoses["kos"] == kos])
    for operator in sorted(points["operator"].dropna().unique()):
        st.subheader(f"{operator} · {kos}")
        p = points[points["operator"] == operator].copy()
        p["ts"] = ctx.local(p["ts"])
        pr = periods[periods["operator"] == operator].copy()
        if not pr.empty:
            pr["start"], pr["end"] = ctx.local(pr["start"]), ctx.local(pr["end"])
        for chart in charts.timeline(p, pr, ctx.cats, ctx.colors.get(operator, charts.SERIES_COLORS[0])):
            _stretch(st.altair_chart, chart)
        if pr.empty:
            st.caption("Tidak ada periode bermasalah pada rentang ini.")
        else:
            table = pd.DataFrame({
                "Mulai": pr["start"].dt.strftime("%d/%m %H:%M"), "Selesai": pr["end"].dt.strftime("%d/%m %H:%M"),
                "Diagnosis": pr["result"],
                "Lama (menit)": ((pr["end"] - pr["start"]).dt.total_seconds() / 60).round().astype(int),
            })
            with st.expander(f"Periode bermasalah ({len(table)})", expanded=len(table) <= 5):
                _table(table)


def page_comparison(ctx: Context) -> None:
    m = ctx.view.measurements
    if m.empty:
        st.info("Tidak ada data untuk pilihan ini.")
        return
    st.subheader("Rekomendasi operator per kos")
    st.caption("Aturan: operator dengan porsi jendela diagnosis \"Normal\" tertinggi; kalau sama, yang median RSRP-nya lebih tinggi.")
    for rec in data.recommendations(ctx.view):
        if rec.best:
            st.success(f"**{rec.kos}: {rec.best}.** {rec.reason}", icon="👍")
        else:
            st.info(f"**{rec.kos}**: belum bisa dibandingkan. {rec.reason}", icon="ℹ️")

    st.subheader("Berapa lama sinyal baik, cukup, atau buruk")
    labels = {name: data.METRIC_LABELS[name][0] for name in ("rsrp", "rsrq", "sinr")}
    metric = st.segmented_control("Besaran", list(labels), format_func=labels.get, default="rsrp",
                                  key="banding_besaran") or "rsrp"
    poor, good = ctx.cats.bounds(metric)
    unit = data.METRIC_LABELS[metric][1]
    st.caption(f"{labels[metric]}: Baik ≥ {num(good, 0, unit)}, Buruk < {num(poor, 0, unit)}, di antaranya Cukup. "
               "Persentase dihitung dari menit yang punya data.")
    shares = data.category_shares(m, metric, ctx.cats)
    if shares.empty:
        st.info(f"Tidak ada data {labels[metric]} (HP tidak memberikannya).")
    else:
        _stretch(st.altair_chart, charts.category_bars(shares, "Persen waktu"))

    st.subheader("Tabel perbandingan")
    table = data.comparison_table(ctx.view, ctx.cats)
    shown = pd.DataFrame({
        "Kos": table["kos"], "Operator": table["operator"],
        "Median RSRP": table["median_rsrp"].map(lambda v: num(v, 0, "dBm")),
        "Median RSRQ": table["median_rsrq"].map(lambda v: num(v, 0, "dB")),
        "Median SINR": table["median_sinr"].map(lambda v: num(v, 1, "dB")),
        "Waktu RSRP buruk": table["pct_rsrp_buruk"].map(lambda v: num(v, 0, "%")),
        "Waktu tanpa layanan": table["pct_tanpa_layanan"].map(lambda v: num(v, 1, "%")),
        "Jendela bermasalah": table["pct_jendela_bermasalah"].map(lambda v: num(v, 0, "%")),
        "Diagnosis dominan": table["diagnosis_dominan"].fillna("–"),
        "Median unduh": table["median_unduh"].map(lambda v: num(v, 1, "Mbps")),
        "Menit data": table["menit_data"],
    })
    _table(shown)
    st.caption("\"Jendela bermasalah\" = porsi jendela diagnosis 10 menit yang hasilnya bukan Normal "
               "(jendela \"Data kurang\" tidak dihitung).")


def page_speed(ctx: Context) -> None:
    s = ctx.view.speedtests
    if s.empty:
        st.info("Belum ada uji kecepatan untuk pilihan ini. Uji kecepatan butuh USB tethering di HP pengukur.")
        return
    ok = s[s["ok"] == 1].copy()
    failed = len(s) - len(ok)
    many_kos = s["kos"].nunique() > 1
    ok["seri"] = ok["kos"] + " · " + ok["operator"] if many_kos else ok["operator"]
    ok["ts"] = ctx.local(ok["ts"])
    palette = charts.series_colors(list(ok["seri"].dropna().unique())) if many_kos else ctx.colors
    st.caption(f"Diukur dari laptop lewat internet HP (USB tethering). Waktu dalam {ctx.tz_label}. "
               f"{len(ok)} uji berhasil, {failed} gagal.")
    c = ctx.cats
    specs = [("download_mbps", "Kecepatan unduh (Mbps)", c.download_good, c.download_poor),
             ("upload_mbps", "Kecepatan unggah (Mbps)", None, None), ("ping_ms", "Ping (ms)", None, None)]
    for field, title, good, poor in specs:
        st.subheader(title)
        if ok[field].notna().sum() == 0:
            st.caption("Belum ada data (unggah hanya diukur pada uji penuh)." if field == "upload_mbps" else "Belum ada data.")
            continue
        _stretch(st.altair_chart, charts.speed_chart(ok, field, title, palette, good, poor))
    if failed:
        bad = s[s["ok"] != 1]
        with st.expander(f"Uji yang gagal ({failed})"):
            _table(pd.DataFrame({"Waktu": ctx.local(bad["ts"]).dt.strftime("%d/%m %H:%M"), "Kos": bad["kos"],
                                 "Operator": bad["operator"], "Jenis": bad["method"], "Penyebab": bad["error"]}))


def _event_text(row: pd.Series) -> str:
    detail = str(row["detail"] or "")
    if row["type"] == db.EVENT_COLLECTOR_ERROR:
        return detail
    old, _, new = detail.split(";")[0].partition(" -> ")
    old = "awal pengukuran" if old.strip() in ("", "-") else old.strip()
    return f"{old} → {new.strip()}" if new else detail


def page_events(ctx: Context) -> None:
    e = ctx.view.events
    network = e[e["type"].isin([db.EVENT_SIGNAL_LOST, db.EVENT_DIAGNOSIS_CHANGED])]
    collector = e[e["type"] == db.EVENT_COLLECTOR_ERROR]
    lost = int((e["type"] == db.EVENT_SIGNAL_LOST).sum())
    c1, c2, c3 = st.columns(3)
    c1.metric("Sinyal putus", lost)
    c2.metric("Perubahan diagnosis", int((e["type"] == db.EVENT_DIAGNOSIS_CHANGED).sum()))
    c3.metric("Gangguan alat ukur", len(collector))
    st.subheader("Sinyal putus dan perubahan diagnosis")
    if network.empty:
        st.info("Tidak ada kejadian pada pilihan ini.")
    else:
        rows = network.sort_values("ts", ascending=False)
        _table(pd.DataFrame({
            f"Waktu ({ctx.tz_label})": ctx.local(rows["ts"]).dt.strftime("%d/%m %H:%M"),
            "Kos": rows["kos"], "Operator": rows["operator"].fillna("–"),
            "Kejadian": rows["type"].map(lambda t: ("⛔ " if t == db.EVENT_SIGNAL_LOST else "🔄 ") + data.EVENT_LABELS[t]),
            "Perubahan": rows.apply(_event_text, axis=1),
        }))
    st.subheader("Gangguan alat ukur")
    st.caption("Ini masalah pada alat ukur (kabel USB lepas, HP terkunci, dsb.), bukan pada jaringan operator. "
               "Ditampilkan supaya jeda data tidak disalahartikan sebagai sinyal hilang.")
    if collector.empty:
        st.info("Tidak ada gangguan alat ukur yang tercatat.")
    else:
        rows = collector.sort_values("ts", ascending=False)
        _table(pd.DataFrame({
            f"Waktu ({ctx.tz_label})": ctx.local(rows["ts"]).dt.strftime("%d/%m %H:%M"),
            "Kos": rows["kos"], "Operator": rows["operator"].fillna("–"), "Keterangan": rows["detail"],
        }))


def page_quality(ctx: Context) -> None:
    quality = data.data_quality(ctx.view.measurements)
    if quality.empty:
        st.info("Tidak ada data untuk pilihan ini.")
        return
    st.caption("Tidak semua HP memberi data yang sama. Halaman ini menunjukkan apa yang tersedia dari tiap HP pengukur, "
               "supaya jelas kesimpulan mana yang kuat dan mana yang terbatas. Nama HP disamarkan.")

    def avail(value) -> str:
        if value is None or pd.isna(value):
            return "–"
        return ("✅ " if value >= 99 else "⚠️ " if value > 0 else "❌ ") + f"{value:.0f}%"

    _table(pd.DataFrame({
        "Kos": quality["kos"], "HP": quality["hp"], "Operator": quality["operator"],
        "RSRP": quality["rsrp"].map(avail), "RSRQ": quality["rsrq"].map(avail), "SINR": quality["sinr"].map(avail),
        "Identitas sel": quality["cell_id"].map(avail), "PCI": quality["pci"].map(avail),
        "Frekuensi (EARFCN)": quality["earfcn"].map(avail), "Band": quality["band"].map(avail),
        "Menit data": quality["menit_data"], "Jeda": quality["jeda"],
    }))
    st.caption("Persen = porsi menit (saat HP mendapat layanan) yang punya nilai itu.")
    for _, row in quality.iterrows():
        with st.container(border=True):
            st.markdown(f"**{row['hp']} · {row['operator']} · {row['kos']}**  \n"
                        f"Data {ctx.fmt(row['mulai'])} s.d. {ctx.fmt(row['selesai'])} {ctx.tz_label}")
            for note in data.quality_notes(row):
                st.markdown(f"- {note}")
    gaps = data.data_gaps(ctx.view.measurements)
    st.subheader("Jeda data")
    if gaps.empty:
        st.info(f"Tidak ada jeda lebih dari {data.GAP_MINUTES} menit.")
    else:
        _table(pd.DataFrame({
            "Kos": gaps["kos"], "HP": gaps["hp"], "Mulai": ctx.local(gaps["mulai"]).dt.strftime("%d/%m %H:%M"),
            "Selesai": ctx.local(gaps["selesai"]).dt.strftime("%d/%m %H:%M"), "Lama (menit)": gaps["menit"].round().astype(int),
        }))


def page_guide(ctx: Context) -> None:
    c, th = ctx.cats, ctx.cfg.diagnosis
    st.markdown(
        "CekSinyal **mengukur dan menjelaskan** sinyal seluler di kos; alat ini tidak memperkuat sinyal. "
        "HP Android pengukur dibaca tiap ±10 detik, lalu tiap 10 menit datanya didiagnosis."
    )
    st.subheader("Tiga angka sinyal")
    _table(pd.DataFrame([
        {"Angka": "RSRP (dBm)", "Artinya": "Kekuatan sinyal yang sampai ke HP. Selalu negatif; makin dekat ke 0 makin kuat.",
         "Baik": f"≥ {num(c.rsrp_good)}", "Cukup": f"{num(c.rsrp_poor)} s.d. {num(c.rsrp_good)}", "Buruk": f"< {num(c.rsrp_poor)}"},
        {"Angka": "RSRQ (dB)", "Artinya": "Kualitas sinyal: seberapa besar porsi sinyal yang berguna dibanding keramaian di frekuensi itu.",
         "Baik": f"≥ {num(c.rsrq_good)}", "Cukup": f"{num(c.rsrq_poor)} s.d. {num(c.rsrq_good)}", "Buruk": f"< {num(c.rsrq_poor)}"},
        {"Angka": "SINR (dB)", "Artinya": "Kebersihan sinyal: perbandingan sinyal dengan gangguan. Di bawah 0 berarti gangguan lebih kuat dari sinyal.",
         "Baik": f"≥ {num(c.sinr_good)}", "Cukup": f"{num(c.sinr_poor)} s.d. {num(c.sinr_good)}", "Buruk": f"< {num(c.sinr_poor)}"},
        {"Angka": "Unduh (Mbps)", "Artinya": "Kecepatan internet yang benar-benar didapat saat diuji.",
         "Baik": f"≥ {num(c.download_good)}", "Cukup": f"{num(c.download_poor)} s.d. {num(c.download_good)}", "Buruk": f"< {num(c.download_poor)}"},
    ]))
    st.caption("Sinyal kuat (RSRP baik) belum tentu internet cepat: kalau SINR buruk atau menaranya ramai, tetap lambat. "
               "Karena itu ketiganya dilihat bersama.")
    st.subheader("Arti tiap diagnosis")
    criteria = {
        rules.LOST: f"≥ {th.signal_lost_fraction:.0%} pembacaan dalam 10 menit tanpa layanan",
        rules.WEAK: f"median RSRP < {num(th.rsrp_weak_dbm, 0, 'dBm')}",
        rules.INTERFERENCE: f"median SINR < {num(th.sinr_interference_db, 0, 'dB')}",
        rules.CONGESTED: f"kecepatan unduh < {th.congestion_ratio:.0%} dari median 24 jam operator itu",
        rules.HANDOVER: f"pindah sel ≥ {th.handover_changes} kali dalam 10 menit",
        rules.NORMAL: "tidak ada syarat di atas yang terpenuhi",
        rules.INSUFFICIENT: f"kurang dari {th.min_samples} pembacaan dalam 10 menit",
    }
    _table(pd.DataFrame([
        {"Diagnosis": f"{diagnosis_icon(name)} {name}", "Artinya": DIAGNOSIS_PLAIN[name], "Syarat": criteria[name],
         "Yang bisa dilakukan": rules.ADVICE[name] if name not in (rules.NORMAL, rules.INSUFFICIENT) else "–"}
        for name in (rules.LOST, rules.WEAK, rules.INTERFERENCE, rules.CONGESTED, rules.HANDOVER, rules.NORMAL, rules.INSUFFICIENT)
    ]))
    st.caption("Syarat diperiksa dari atas ke bawah dan berhenti di yang pertama terpenuhi. Kalau HP tidak memberi data "
               "untuk suatu syarat, syarat itu dilewati dan dicatat (lihat tab Kualitas data), tidak ditebak.")
    st.subheader("Catatan")
    st.markdown(
        f"- Semua waktu ditampilkan dalam **{ctx.tz_label}**.\n"
        "- Tiap titik data di dashboard adalah rata-rata satu menit dari satu HP pengukur.\n"
        "- Hasil hanya berlaku untuk titik tempat HP pengukur diletakkan; kamar lain di kos yang sama bisa berbeda.\n"
        "- Nama kos dan HP disamarkan untuk menjaga privasi penghuni."
    )


PAGES = [page_summary, page_timeline, page_comparison, page_speed, page_events, page_quality, page_guide]


# ---------------------------------------------------------------- kerangka

def _sidebar(ds: Dataset, s: settings_mod.DashboardSettings) -> Dataset:
    st.sidebar.header("Saring")
    kos_names = ds.kos_names()
    kos = st.sidebar.multiselect("Kos", kos_names, default=kos_names, key="filter_kos")
    operators_all = sorted(ds.measurements["operator"].dropna().unique())
    operators = st.sidebar.multiselect("Operator", operators_all, default=operators_all, key="filter_operator")
    choice = st.sidebar.selectbox("Rentang waktu", list(RANGES), index=0, key="filter_rentang",
                                  help="Dihitung mundur dari data terbaru yang ada.")
    latest = ds.measurements["ts"].max()
    start = None if RANGES[choice] is None else (latest - timedelta(hours=RANGES[choice])).to_pydatetime()
    if st.sidebar.button("Muat ulang data", help="Data juga dimuat ulang otomatis tiap beberapa menit."):
        st.cache_data.clear()
        st.rerun()
    st.sidebar.caption(f"Sumber: {ds.source}")
    if s.public:
        st.sidebar.caption("Mode publik: nama kos dan HP disamarkan.")
    return ds.filter(kos=kos or kos_names, operators=operators or operators_all, start=start)


def main() -> None:
    st.set_page_config(page_title="CekSinyal", page_icon="📶", layout="wide")
    st.title("📶 CekSinyal")
    st.caption("Seberapa bagus internet seluler di kos, dan kenapa. "
               "Tugas besar ET4132 Kapita Selekta Teknik Telekomunikasi, ITB (Kelompok 2).")
    s = settings_mod.resolve(_secrets(), os.environ)
    problem = s.problem()
    if problem:
        st.error(f"Dashboard belum bisa memuat data. {problem}")
        st.stop()
    try:
        raw = _load(s)
    except Exception as exc:
        # Nama kelas saja: pesan exception pustaka bisa memuat detail yang tidak perlu tampil ke publik.
        st.error(f"Gagal memuat data dari sumber ({type(exc).__name__}). Coba muat ulang beberapa menit lagi.")
        st.stop()
    full = data.apply_privacy(raw, s.aliases, s.public)
    if full.empty:
        st.info("Belum ada data pengukuran di sumber data ini.")
        st.stop()
    now = datetime.now(timezone.utc)
    ctx = Context(full, _sidebar(full, s), s, now)
    st.caption(f"Data terbaru: {ctx.fmt(full.measurements['ts'].max(), '%d/%m/%Y %H:%M')} {ctx.tz_label} · "
               f"dimuat {ctx.fmt(raw.loaded_at, '%H:%M')} {ctx.tz_label}")
    view = st.segmented_control("Tampilan", VIEWS, default=VIEW_HOME, required=True, key="tampilan",
                                label_visibility="collapsed") or VIEW_HOME
    if view == VIEW_HOME:
        page_home(ctx)
        return
    for tab, page in zip(st.tabs(TABS), PAGES):
        with tab:
            page(ctx)
