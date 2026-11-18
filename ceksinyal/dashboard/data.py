"""Lapisan data dashboard: memuat dari SQLite atau Google Sheets ke skema DataFrame yang sama,
lalu transformasi murni (kategori, ringkasan, perbandingan, kualitas data, alias privasi).

Modul ini hanya butuh pandas; tidak mengimpor streamlit, jadi bisa dites tanpa menjalankan app.

Skema bersama (semua waktu = datetime UTC ber-zona; tampilan mengonversinya ke WIB):
- measurements: SATU BARIS PER MENIT PER HP. Sheets sudah per menit; SQLite (per sampel) diagregasi
  dengan fungsi yang sama dengan uploader, supaya kedua sumber identik.
- speedtests, diagnoses, events: satu baris per uji / jendela / kejadian.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from .. import db
from ..config import Config, split_sim_key
from ..diagnosis import rules
from ..uploader.sheets import MINUTE_HEADERS, aggregate_minutes

MEASUREMENT_COLUMNS = [
    "ts", "kos", "device", "operator", "n_samples", "n_no_service", "n_parse_failed", "rat",
    "rsrp", "rsrp_min", "rsrq", "sinr", "pci", "cell_id", "earfcn", "band",
]
SPEEDTEST_COLUMNS = ["ts", "kos", "operator", "method", "ping_ms", "download_mbps", "upload_mbps", "ok", "error"]
DIAGNOSIS_COLUMNS = [
    "window_start", "window_end", "kos", "operator", "n_samples", "median_rsrp", "median_sinr",
    "median_download", "cell_changes", "result", "advice", "detail",
]
EVENT_COLUMNS = ["ts", "kos", "operator", "type", "detail"]

_NUMERIC = {
    "measurements": ["n_samples", "n_no_service", "n_parse_failed", "rsrp", "rsrp_min", "rsrq", "sinr",
                     "pci", "cell_id", "earfcn", "band"],
    "speedtests": ["ping_ms", "download_mbps", "upload_mbps", "ok"],
    "diagnoses": ["n_samples", "median_rsrp", "median_sinr", "median_download", "cell_changes"],
    "events": [],
}
_TIMES = {
    "measurements": ["ts"], "speedtests": ["ts"], "diagnoses": ["window_start", "window_end"], "events": ["ts"],
}
_COLUMNS = {
    "measurements": MEASUREMENT_COLUMNS, "speedtests": SPEEDTEST_COLUMNS,
    "diagnoses": DIAGNOSIS_COLUMNS, "events": EVENT_COLUMNS,
}
# Nama kolom di tab Sheets / SQLite -> nama di skema bersama.
_RENAME = {"ts_minute": "ts", "device_serial": "device", "rsrp_avg": "rsrp", "rsrq_avg": "rsrq", "sinr_avg": "sinr"}

GOOD, FAIR, POOR, NO_DATA = "Baik", "Cukup", "Buruk", "Tidak ada data"
CATEGORY_ORDER = (GOOD, FAIR, POOR)
EVENT_LABELS = {
    db.EVENT_SIGNAL_LOST: "Sinyal putus",
    db.EVENT_DIAGNOSIS_CHANGED: "Diagnosis berubah",
    db.EVENT_COLLECTOR_ERROR: "Gangguan alat ukur",
}
GAP_MINUTES = 5  # jeda antar-menit data yang dianggap "data bolong"
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


@dataclass
class Dataset:
    measurements: pd.DataFrame
    speedtests: pd.DataFrame
    diagnoses: pd.DataFrame
    events: pd.DataFrame
    source: str = ""
    loaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def empty(self) -> bool:
        return self.measurements.empty

    def kos_names(self) -> list[str]:
        return sorted(self.measurements["kos"].dropna().unique())

    def filter(self, kos: Iterable[str] | None = None, operators: Iterable[str] | None = None,
               start: datetime | None = None, end: datetime | None = None) -> "Dataset":
        """Saring semua tabel dengan kos/operator/rentang waktu yang sama."""
        def pick(df: pd.DataFrame, time_col: str, end_col: str | None = None) -> pd.DataFrame:
            mask = pd.Series(True, index=df.index)
            if kos is not None:
                mask &= df["kos"].isin(list(kos))
            if operators is not None:
                # kejadian tanpa operator (mis. adb hilang) tetap ikut
                mask &= df["operator"].isin(list(operators)) | df["operator"].isna()
            if start is not None:
                mask &= df[end_col or time_col] >= start
            if end is not None:
                mask &= df[time_col] <= end
            return df[mask]

        return replace(
            self,
            measurements=pick(self.measurements, "ts"),
            speedtests=pick(self.speedtests, "ts"),
            diagnoses=pick(self.diagnoses, "window_start", "window_end"),
            events=pick(self.events, "ts"),
        )


# ---------------------------------------------------------------- normalisasi

_DECIMAL_COMMA = re.compile(r"^\s*-?\d+,\d+\s*$")


def _to_number(values: pd.Series) -> pd.Series:
    """Angka atau teks angka -> float. Teks berkoma desimal ("25,68", locale Indonesia di Sheets) juga diterima."""
    if values.dtype == object:
        values = values.map(lambda v: v.replace(",", ".") if isinstance(v, str) and _DECIMAL_COMMA.match(v) else v)
    return pd.to_numeric(values, errors="coerce")


def _frame(records: list[dict[str, Any]], table: str) -> pd.DataFrame:
    """Daftar dict (dari SQLite atau Sheets) -> DataFrame berskema tetap dengan tipe yang benar."""
    columns = _COLUMNS[table]
    df = pd.DataFrame.from_records(records) if records else pd.DataFrame()
    df = df.rename(columns=_RENAME)
    keep_id = "id" in df.columns
    for col in columns:
        if col not in df.columns:
            df[col] = None
    df = df[(["id"] if keep_id else []) + columns].copy()
    for col in columns:
        if col in _TIMES[table]:
            df[col] = pd.to_datetime(df[col], utc=True, errors="coerce", format="ISO8601")
        elif col in _NUMERIC[table]:
            df[col] = _to_number(df[col])
        else:  # teks: sel kosong dari Sheets ("") dianggap tidak ada
            df[col] = df[col].astype(object).where(df[col].notna() & (df[col].astype(str).str.strip() != ""), None)
    first_time = _TIMES[table][0]
    df = df[df[first_time].notna() & df["kos"].notna()]

    # Uploader menjamin "minimal sekali", dan `diagnose --rerun` mengirim ulang: buang baris ganda.
    if table == "measurements":
        df = df.drop_duplicates(["ts", "kos", "device", "operator"], keep="last")
    elif table == "diagnoses":
        df = df.drop_duplicates(["kos", "operator", "window_start"], keep="last")
    elif keep_id:
        df = df.drop_duplicates(["id", "kos", first_time], keep="last")
    df = df.drop(columns=["id"], errors="ignore")
    return df.sort_values(first_time, kind="stable").reset_index(drop=True)


def _raw_to_minutes(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Baris measurements per sampel (dict) -> baris per menit, lewat agregasi milik uploader."""
    rows = []
    for r in records:
        if not r.get("ts") or len(str(r["ts"])) < 16:
            continue
        row = dict(r)
        for key in ("raw_ok", "rsrp", "rsrq", "sinr", "pci", "cell_id", "earfcn", "band"):
            value = row.get(key)
            if value == "" or value is None:
                row[key] = None
            else:
                try:
                    number = float(value)
                    row[key] = int(number) if number.is_integer() and key != "sinr" else number
                except (TypeError, ValueError):
                    row[key] = None
        row["ts"] = str(row["ts"])
        row["rat"] = row.get("rat") or None
        row.setdefault("id", 0)
        rows.append(row)
    return [dict(zip(MINUTE_HEADERS, values)) for values, _ in aggregate_minutes(rows)]


# ---------------------------------------------------------------- pemuat

def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def load_sqlite(path: Path | str) -> Dataset:
    """Baca DB lokal dalam mode hanya-baca (tanpa migrasi; DB lama dengan kolom kurang tetap bisa dibuka)."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"database tidak ditemukan: {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        def records(table: str, wanted: list[str], where: str = "") -> list[dict[str, Any]]:
            have = _table_columns(conn, table)
            if not have:
                return []
            cols = [c for c in wanted if c in have]
            return [dict(r) for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table} {where} ORDER BY id")]

        raw = records("measurements", ["id", "ts", "kos", "device_serial", "operator", "rat", "rsrp", "rsrq", "sinr",
                                       "pci", "cell_id", "earfcn", "band", "raw_ok"])
        types = ", ".join(f"'{t}'" for t in EVENT_LABELS)
        return Dataset(
            measurements=_frame(_raw_to_minutes(raw), "measurements"),
            speedtests=_frame(records("speedtests", ["id", *SPEEDTEST_COLUMNS]), "speedtests"),
            diagnoses=_frame(records("diagnoses", ["id", *DIAGNOSIS_COLUMNS]), "diagnoses"),
            events=_frame(records("events", ["id", *EVENT_COLUMNS], f"WHERE type IN ({types})"), "events"),
            source=f"SQLite: {path.name}",
        )
    finally:
        conn.close()


def load_sheets(read_tab: Callable[[str], list[list[Any]]], source: str = "Google Sheets") -> Dataset:
    """Baca empat tab dari spreadsheet bersama. `read_tab(nama)` mengembalikan semua baris (baris 1 = judul),
    atau daftar kosong kalau tab belum ada."""
    def records(tab: str) -> list[dict[str, Any]]:
        rows = read_tab(tab) or []
        if len(rows) < 2:
            return []
        headers = [str(h).strip() for h in rows[0]]
        return [dict(zip(headers, row)) for row in rows[1:] if any(str(c).strip() for c in row)]

    measurements = records("measurements")
    if measurements and "ts_minute" not in measurements[0]:
        # Spreadsheet diisi dengan sheets.aggregate_measurements: false (per sampel): samakan ke per menit.
        measurements = _raw_to_minutes(measurements)
    events = [e for e in records("events") if e.get("type") in EVENT_LABELS]
    return Dataset(
        measurements=_frame(measurements, "measurements"),
        speedtests=_frame(records("speedtests"), "speedtests"),
        diagnoses=_frame(records("diagnoses"), "diagnoses"),
        events=_frame(events, "events"),
        source=source,
    )


def gspread_reader(spreadsheet: Any) -> Callable[[str], list[list[Any]]]:
    """Pembaca tab untuk objek Spreadsheet gspread (satu permintaan API per tab).

    Nilai dibaca mentah (UNFORMATTED_VALUE): tanpa itu, spreadsheet ber-locale Indonesia mengembalikan angka
    sebagai teks berkoma desimal ("25,68") yang tidak terbaca sebagai angka."""
    def read(tab: str) -> list[list[Any]]:
        import gspread

        try:
            return spreadsheet.worksheet(tab).get_all_values(value_render_option="UNFORMATTED_VALUE")
        except gspread.WorksheetNotFound:
            return []
    return read


# ---------------------------------------------------------------- privasi

def kos_alias_map(names: Iterable[str], aliases: dict[str, str], public: bool) -> dict[str, str]:
    """Nama kos asli -> nama tampil. Mode publik: SELALU alias; kos tanpa alias mendapat "Kos 1", "Kos 2", ..."""
    mapping = {}
    used = set(aliases.values())
    counter = 0
    for name in sorted(set(names)):
        if name in aliases:
            mapping[name] = aliases[name]
        elif not public:
            mapping[name] = name
        else:
            counter += 1
            while f"Kos {counter}" in used:
                counter += 1
            mapping[name] = f"Kos {counter}"
            used.add(mapping[name])
    return mapping


def device_label_map(measurements: pd.DataFrame) -> dict[str, str]:
    """Serial ADB -> "HP 1", "HP 2", ... (urut kos, operator, lalu serial, supaya stabil antar-muat).

    HP dual-SIM yang dibaca dua slot ("SERIAL/sim1", "SERIAL/sim2") tetap satu HP: "HP 1 · SIM 1" dan
    "HP 1 · SIM 2". Serial dasarnya juga dipetakan ke "HP 1" supaya tersamar di teks kejadian.
    """
    if measurements.empty:
        return {}
    devices = measurements.dropna(subset=["device"]).copy()
    devices["base"] = devices["device"].map(lambda d: split_sim_key(str(d))[0])
    firsts = devices.groupby("base", sort=False).agg(kos=("kos", "first"), operator=("operator", "first")).reset_index()
    firsts = firsts.sort_values(["kos", "operator", "base"], na_position="last")
    base_label = {base: f"HP {i}" for i, base in enumerate(firsts["base"], start=1)}
    mapping: dict[str, str] = {}
    for key in devices["device"].unique():
        base, slot = split_sim_key(str(key))
        mapping[key] = base_label[base] if slot is None else f"{base_label[base]} · SIM {slot}"
    for base, label in base_label.items():
        mapping.setdefault(base, label)
    return mapping


def apply_privacy(ds: Dataset, aliases: dict[str, str], public: bool) -> Dataset:
    """Ganti nama kos dengan alias dan serial HP dengan label. Serial asli tidak pernah sampai ke tampilan."""
    names = set()
    for df in (ds.measurements, ds.speedtests, ds.diagnoses, ds.events):
        names |= set(df["kos"].dropna().unique())
    kos_map = kos_alias_map(names, aliases, public)
    devices = device_label_map(ds.measurements)

    def rename_kos(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["kos"] = out["kos"].map(lambda k: kos_map.get(k, k))
        return out

    measurements = rename_kos(ds.measurements)
    measurements["device"] = measurements["device"].map(lambda d: devices.get(d, "HP ?"))

    def scrub(text: Any) -> Any:
        if not isinstance(text, str):
            return text
        # Terpanjang dulu: "SERIAL/sim1" harus diganti utuh sebelum "SERIAL".
        for serial, label in sorted(devices.items(), key=lambda kv: -len(str(kv[0]))):
            text = text.replace(str(serial), label)
        for real, alias in kos_map.items():
            if real != alias:
                text = text.replace(real, alias)
        return text

    events = rename_kos(ds.events)
    if not events.empty:
        events["detail"] = events["detail"].map(scrub)
        # Rincian gangguan alat ukur bisa memuat path file di laptop anggota: tidak ditampilkan.
        collector = events["type"] == db.EVENT_COLLECTOR_ERROR
        events.loc[collector, "detail"] = events.loc[collector, "detail"].map(_collector_summary)
    speedtests = rename_kos(ds.speedtests)
    if not speedtests.empty:
        # Pesan error uji kecepatan bisa memuat IP lokal laptop ("IP sumber 192.168.42.129 ...").
        speedtests["error"] = speedtests["error"].map(
            lambda t: _IPV4.sub("(IP)", scrub(t)) if isinstance(t, str) else t)
    return replace(ds, measurements=measurements, speedtests=speedtests,
                   diagnoses=rename_kos(ds.diagnoses), events=events)


def _collector_summary(detail: Any) -> str:
    text = str(detail or "").lower()
    if "terputus" in text or "belum terhubung" in text:
        return "HP terputus dari laptop"
    if "parsing gagal" in text:
        return "Data dari HP tidak bisa dibaca (parsing gagal)"
    if "adb" in text:
        return "ADB di laptop bermasalah"
    if "rssnr" in text:
        return "Satuan SINR HP disesuaikan otomatis"
    return "Gangguan pada alat ukur"


# ---------------------------------------------------------------- kategori

@dataclass
class Categories:
    """Batas Baik/Cukup/Buruk. Batas "Buruk" RSRP dan SINR = ambang diagnosis (satu sumber kebenaran)."""

    rsrp_good: float = -100.0
    rsrp_poor: float = -110.0
    rsrq_good: float = -10.0
    rsrq_poor: float = -15.0
    sinr_good: float = 10.0
    sinr_poor: float = 0.0
    download_good: float = 10.0
    download_poor: float = 2.0

    @classmethod
    def from_config(cls, cfg: Config) -> "Categories":
        d, th = cfg.dashboard, cfg.diagnosis
        return cls(
            rsrp_good=max(d.rsrp_good_dbm, th.rsrp_weak_dbm), rsrp_poor=th.rsrp_weak_dbm,
            rsrq_good=max(d.rsrq_good_db, d.rsrq_poor_db), rsrq_poor=d.rsrq_poor_db,
            sinr_good=max(d.sinr_good_db, th.sinr_interference_db), sinr_poor=th.sinr_interference_db,
            download_good=max(d.download_good_mbps, d.download_poor_mbps), download_poor=d.download_poor_mbps,
        )

    def bounds(self, metric: str) -> tuple[float, float]:
        """(batas buruk, batas baik) untuk rsrp / rsrq / sinr / download."""
        return getattr(self, f"{metric}_poor"), getattr(self, f"{metric}_good")


METRIC_LABELS = {"rsrp": ("RSRP", "dBm"), "rsrq": ("RSRQ", "dB"), "sinr": ("SINR", "dB"),
                 "download": ("Unduh", "Mbps")}


def categorize(value: Any, metric: str, cats: Categories) -> str:
    """Baik kalau >= batas baik, Buruk kalau < batas buruk, selain itu Cukup."""
    if value is None or pd.isna(value):
        return NO_DATA
    poor, good = cats.bounds(metric)
    if value < poor:
        return POOR
    return GOOD if value >= good else FAIR


def categorize_series(values: pd.Series, metric: str, cats: Categories) -> pd.Series:
    return values.map(lambda v: categorize(v, metric, cats))


# ---------------------------------------------------------------- ringkasan

@dataclass
class Card:
    kos: str
    operator: str
    last_seen: datetime
    minutes_ago: float
    stale: bool
    medians: dict[str, float | None]      # rsrp, rsrq, sinr (24 jam sebelum data terakhir)
    categories: dict[str, str]
    pct_no_service: float | None
    diagnosis: str | None = None
    diagnosis_end: datetime | None = None
    advice: str | None = None
    detail: str | None = None
    download: float | None = None
    download_ts: datetime | None = None
    download_category: str = NO_DATA
    level: "Level | None" = None


def _median(series: pd.Series) -> float | None:
    values = series.dropna()
    return float(values.median()) if len(values) else None


def summary_cards(ds: Dataset, cats: Categories, now: datetime, stale_minutes: float = 15.0,
                  level_window_minutes: float = 15.0) -> list[Card]:
    """Satu kartu per kos x operator. Median dihitung dari 24 jam sebelum data terakhir pasangan itu,
    supaya data lama (collector sudah berhenti) tetap terbaca, dengan peringatan "diam"."""
    cards = []
    m = ds.measurements.dropna(subset=["operator"])
    for (kos, operator), group in m.groupby(["kos", "operator"], sort=True):
        last_seen = group["ts"].max().to_pydatetime()
        recent = group[group["ts"] > last_seen - timedelta(hours=24)]
        medians = {name: _median(recent[name]) for name in ("rsrp", "rsrq", "sinr")}
        if medians["sinr"] == 0 and (recent["sinr"].dropna() == 0).all():
            medians["sinr"] = None  # selalu persis 0 = nilai kosong dari HP (lihat aturan diagnosis)
        samples = recent["n_samples"].sum()
        minutes_ago = max(0.0, (now - last_seen).total_seconds() / 60 - 1)  # baris = awal menit
        card = Card(
            kos=kos, operator=operator, last_seen=last_seen, minutes_ago=minutes_ago,
            stale=minutes_ago > stale_minutes, medians=medians,
            categories={name: categorize(value, name, cats) for name, value in medians.items()},
            pct_no_service=float(100 * recent["n_no_service"].sum() / samples) if samples else None,
        )
        d = ds.diagnoses[(ds.diagnoses["kos"] == kos) & (ds.diagnoses["operator"] == operator)]
        if not d.empty:
            last = d.iloc[-1]
            card.diagnosis, card.diagnosis_end = last["result"], last["window_end"].to_pydatetime()
            card.advice, card.detail = last["advice"], last["detail"]
        s = ds.speedtests[(ds.speedtests["kos"] == kos) & (ds.speedtests["operator"] == operator)
                          & (ds.speedtests["ok"] == 1) & ds.speedtests["download_mbps"].notna()]
        if not s.empty:
            card.download, card.download_ts = float(s.iloc[-1]["download_mbps"]), s.iloc[-1]["ts"].to_pydatetime()
            card.download_category = categorize(card.download, "download", cats)
        card.level = quality_level(group, s, cats, level_window_minutes)
        cards.append(card)
    return cards


# ---------------------------------------------------------------- level 1-5 (Beranda)

# Level seperti bar sinyal HP, tetapi untuk KUALITAS (bukan hanya kekuatan). 0 = tidak ada layanan.
LEVEL_NAMES = {5: "Sangat baik", 4: "Baik", 3: "Cukup", 2: "Buruk", 1: "Sangat buruk", 0: "Tidak ada sinyal"}
LEVEL_USES = {
    5: "Lancar untuk video call, streaming HD, dan game online.",
    4: "Lancar untuk streaming video dan video call.",
    3: "Cukup untuk chat, media sosial, dan video kualitas sedang. Video call bisa tersendat.",
    2: "Hanya nyaman untuk chat dan pesan. Video sering buffering.",
    1: "Sering gagal tersambung. Telepon dan pesan pun bisa terganggu.",
    0: "HP tidak mendapat layanan dari operator ini di titik ini.",
}
# Penjelasan awam untuk besaran yang paling menurunkan level.
LEVEL_CAUSES = {
    "rsrp": "Sinyal dari menara lemah sampai di titik ini.",
    "sinr": "Sinyal terganggu oleh sinyal lain (tidak bersih).",
    "download": "Sinyalnya ada, tetapi internet sedang lambat (menara ramai).",
}
SPEED_FRESH_MINUTES = 30  # uji kecepatan lebih lama dari ini sebelum data terakhir tidak ikut menentukan level
LEVEL_METRICS = ("rsrp", "sinr", "download")


@dataclass
class Level:
    level: int | None                      # 0-5; None = belum bisa dinilai
    by_metric: dict[str, int]              # level tiap besaran yang tersedia
    limiting: str | None = None            # besaran yang paling menurunkan level (rsrp/sinr/download)
    window_minutes: float = 15.0
    pct_no_service: float | None = None

    @property
    def name(self) -> str:
        return LEVEL_NAMES.get(self.level, "Belum bisa dinilai") if self.level is not None else "Belum bisa dinilai"

    @property
    def uses(self) -> str:
        return LEVEL_USES.get(self.level, "Data terbaru terlalu sedikit untuk dinilai.") \
            if self.level is not None else "Data terbaru terlalu sedikit untuk dinilai."

    @property
    def cause(self) -> str | None:
        """Penyebab yang ditampilkan hanya kalau level di bawah Baik."""
        if self.level is None or self.level >= 4 or self.level == 0 or self.limiting is None:
            return None
        return LEVEL_CAUSES[self.limiting]


def level_cuts(metric: str, cats: Categories) -> tuple[float, float, float, float]:
    """Empat batas naik untuk level 2, 3, 4, 5. Batas level 3 = "Buruk", batas level 4 = "Baik" (sama dengan
    kategori Baik/Cukup/Buruk), sehingga level 4-5 selalu Baik, 3 Cukup, 1-2 Buruk."""
    poor, good = cats.bounds(metric)
    if metric == "download":  # kecepatan: kelipatan, bukan selisih tetap
        return poor / 2, poor, good, good * 2
    step = good - poor
    return poor - step, poor, good, good + step


def metric_level(value: Any, metric: str, cats: Categories) -> int | None:
    if value is None or pd.isna(value):
        return None
    return 1 + sum(value >= cut for cut in level_cuts(metric, cats))


def quality_level(measurements: pd.DataFrame, speedtests: pd.DataFrame, cats: Categories,
                  window_minutes: float = 15.0) -> Level:
    """Level 1-5 untuk satu kos x operator dari data `window_minutes` terakhir (sebelum data terakhirnya).

    Level = besaran terlemah di antara RSRP, SINR, dan kecepatan unduh terbaru (kalau ada): koneksi hanya sebaik
    mata rantai terlemahnya. Tanpa layanan di >= setengah sampel -> level 0.
    """
    if measurements.empty:
        return Level(None, {}, window_minutes=window_minutes)
    last = measurements["ts"].max()
    recent = measurements[measurements["ts"] > last - timedelta(minutes=window_minutes)]
    samples = recent["n_samples"].sum()
    no_service = float(100 * recent["n_no_service"].sum() / samples) if samples else None
    if no_service is not None and no_service >= 50:
        return Level(0, {}, window_minutes=window_minutes, pct_no_service=no_service)
    values: dict[str, float | None] = {"rsrp": _median(recent["rsrp"]), "sinr": _median(recent["sinr"])}
    if values["sinr"] == 0 and (recent["sinr"].dropna() == 0).all():
        values["sinr"] = None  # selalu persis 0 = nilai kosong dari HP
    fresh = speedtests[speedtests["ts"] > last - timedelta(minutes=SPEED_FRESH_MINUTES)] if not speedtests.empty \
        else speedtests
    values["download"] = float(fresh.iloc[-1]["download_mbps"]) if not fresh.empty else None
    by_metric = {m: lvl for m in LEVEL_METRICS if (lvl := metric_level(values[m], m, cats)) is not None}
    if "rsrp" not in by_metric and "sinr" not in by_metric:
        return Level(None, by_metric, window_minutes=window_minutes, pct_no_service=no_service)
    limiting = min(by_metric, key=lambda m: (by_metric[m], LEVEL_METRICS.index(m)))
    return Level(by_metric[limiting], by_metric, limiting, window_minutes, no_service)


def format_ago(minutes: float) -> str:
    if minutes < 1.5:
        return "baru saja"
    if minutes < 90:
        return f"{minutes:.0f} menit lalu"
    if minutes < 48 * 60:
        return f"{minutes / 60:.0f} jam lalu"
    return f"{minutes / 1440:.0f} hari lalu"


# ---------------------------------------------------------------- linimasa

def problem_periods(diagnoses: pd.DataFrame) -> pd.DataFrame:
    """Jendela diagnosis bermasalah yang berurutan dan sama hasilnya digabung jadi satu periode."""
    columns = ["kos", "operator", "start", "end", "result", "windows"]
    rows = []
    bad = diagnoses[diagnoses["result"].isin(rules.PROBLEMS)]
    for (kos, operator), group in bad.groupby(["kos", "operator"], sort=True):
        current = None
        for row in group.sort_values("window_start").itertuples():
            if current and current["result"] == row.result and row.window_start <= current["end"]:
                current["end"] = row.window_end
                current["windows"] += 1
            else:
                if current:
                    rows.append(current)
                current = {"kos": kos, "operator": operator, "start": row.window_start, "end": row.window_end,
                           "result": row.result, "windows": 1}
        if current:
            rows.append(current)
    return pd.DataFrame(rows, columns=columns)


def downsample(measurements: pd.DataFrame, max_points: int = 600) -> pd.DataFrame:
    """Rata-rata per ember waktu per kos x operator supaya grafik tetap ringan di HP. Jeda data tetap jadi celah."""
    cols = ["ts", "kos", "operator", "rsrp", "rsrq", "sinr"]
    if measurements.empty:
        return measurements[cols]
    span = (measurements["ts"].max() - measurements["ts"].min()).total_seconds() / 60
    step = 1
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 360, 1440):
        if span / step <= max_points:
            break
    df = measurements[cols].copy()
    df["ts"] = df["ts"].dt.floor(f"{step}min")
    out = df.groupby(["kos", "operator", "ts"], as_index=False, sort=True)[["rsrp", "rsrq", "sinr"]].mean()
    out[["rsrp", "rsrq", "sinr"]] = out[["rsrp", "rsrq", "sinr"]].round(1)
    # Sisipkan baris kosong di tiap jeda supaya garis terputus, bukan menyambung lurus melewati data bolong.
    gap = pd.Timedelta(minutes=max(step * 3, GAP_MINUTES))
    pieces = []
    for (kos, operator), group in out.groupby(["kos", "operator"], sort=False):
        breaks = group[group["ts"].diff() > gap]
        if not breaks.empty:
            filler = pd.DataFrame({"kos": kos, "operator": operator, "ts": breaks["ts"] - pd.Timedelta(minutes=step),
                                   "rsrp": None, "rsrq": None, "sinr": None})
            group = pd.concat([group, filler.astype(group.dtypes.to_dict())]).sort_values("ts", kind="stable")
        pieces.append(group)
    return pd.concat(pieces, ignore_index=True)[cols]


# ---------------------------------------------------------------- perbandingan

def category_shares(measurements: pd.DataFrame, metric: str, cats: Categories) -> pd.DataFrame:
    """Persentase menit Baik/Cukup/Buruk per kos x operator untuk satu besaran."""
    rows = []
    valid = measurements.dropna(subset=[metric, "operator"])
    for (kos, operator), group in valid.groupby(["kos", "operator"], sort=True):
        counts = categorize_series(group[metric], metric, cats).value_counts()
        total = int(counts.sum())
        for order, name in enumerate(CATEGORY_ORDER):
            rows.append({"kos": kos, "operator": operator, "kategori": name, "urutan": order,
                         "menit": int(counts.get(name, 0)), "persen": round(100 * counts.get(name, 0) / total, 1)})
    return pd.DataFrame(rows, columns=["kos", "operator", "kategori", "urutan", "menit", "persen"])


def comparison_table(ds: Dataset, cats: Categories) -> pd.DataFrame:
    """Per kos x operator: median tiap besaran, persentase waktu buruk, dan porsi jendela bermasalah."""
    rows = []
    m = ds.measurements.dropna(subset=["operator"])
    for (kos, operator), group in m.groupby(["kos", "operator"], sort=True):
        samples = group["n_samples"].sum()
        rsrp = group["rsrp"].dropna()
        d = ds.diagnoses[(ds.diagnoses["kos"] == kos) & (ds.diagnoses["operator"] == operator)]
        conclusive = d[d["result"] != rules.INSUFFICIENT]
        s = ds.speedtests[(ds.speedtests["kos"] == kos) & (ds.speedtests["operator"] == operator)
                          & (ds.speedtests["ok"] == 1)]
        rows.append({
            "kos": kos, "operator": operator, "menit_data": len(group),
            "median_rsrp": _median(group["rsrp"]), "median_rsrq": _median(group["rsrq"]),
            "median_sinr": _median(group["sinr"]),
            "pct_rsrp_buruk": float(100 * (rsrp < cats.rsrp_poor).mean()) if len(rsrp) else None,
            "pct_rsrp_tidak_baik": float(100 * (rsrp < cats.rsrp_good).mean()) if len(rsrp) else None,
            "pct_tanpa_layanan": float(100 * group["n_no_service"].sum() / samples) if samples else None,
            "jendela": len(conclusive),
            "pct_jendela_bermasalah": float(100 * conclusive["result"].isin(rules.PROBLEMS).mean()) if len(conclusive) else None,
            "diagnosis_dominan": _dominant(conclusive["result"]),
            "median_unduh": _median(s["download_mbps"]),
        })
    return pd.DataFrame(rows)


def _dominant(results: pd.Series) -> str | None:
    counts = results.value_counts()
    if counts.empty:
        return None
    # seri -> yang lebih parah (sama dengan engine.summarize_operators)
    return max(counts.index, key=lambda r: (counts[r], rules.SEVERITY.index(r) if r in rules.SEVERITY else -1))


@dataclass
class Recommendation:
    kos: str
    best: str | None
    reason: str


def recommendations(ds: Dataset) -> list[Recommendation]:
    """Operator terbaik per kos, aturan sama dengan CLAUDE.md / diagnosis.engine.recommend:
    porsi jendela Normal tertinggi, lalu median RSRP tertinggi."""
    out = []
    for kos in sorted(ds.diagnoses["kos"].dropna().unique()) or []:
        ranked = []
        d = ds.diagnoses[(ds.diagnoses["kos"] == kos) & (ds.diagnoses["result"] != rules.INSUFFICIENT)]
        for operator, group in d.dropna(subset=["operator"]).groupby("operator", sort=True):
            m = ds.measurements[(ds.measurements["kos"] == kos) & (ds.measurements["operator"] == operator)]
            ranked.append((operator, float((group["result"] == rules.NORMAL).mean()), _median(m["rsrp"])))
        if len(ranked) < 2:
            out.append(Recommendation(kos, None, "Butuh minimal dua operator dengan diagnosis di kos ini "
                                                 f"(tersedia: {len(ranked)})."))
            continue
        ranked.sort(key=lambda r: (-r[1], -(r[2] if r[2] is not None else -999)))
        (best, share, rsrp), (second, share2, rsrp2) = ranked[0], ranked[1]
        if share != share2:
            reason = f"{share:.0%} jendela Normal (dibanding {second} {share2:.0%})."
        else:
            reason = (f"Diagnosis setara ({share:.0%} jendela Normal); median RSRP lebih tinggi "
                      f"({_dbm(rsrp)} dibanding {second} {_dbm(rsrp2)}).")
        out.append(Recommendation(kos, best, reason))
    return out


def _dbm(value: float | None) -> str:
    return "–" if value is None else f"{value:.0f} dBm".replace("-", "−")


# ---------------------------------------------------------------- kualitas data

QUALITY_FIELDS = ("rsrp", "rsrq", "sinr", "pci", "cell_id", "earfcn", "band")


def data_quality(measurements: pd.DataFrame) -> pd.DataFrame:
    """Per HP: berapa persen menit yang punya tiap kolom, jumlah jeda, dan pembacaan yang gagal di-parse."""
    rows = []
    for (kos, device), group in measurements.groupby(["kos", "device"], sort=True):
        served = group[(group["n_samples"] - group["n_no_service"]) > 0]
        row = {
            "kos": kos, "hp": device, "operator": ", ".join(sorted(group["operator"].dropna().unique())),
            "mulai": group["ts"].min(), "selesai": group["ts"].max(), "menit_data": len(group),
            "gagal_parse": int(group["n_parse_failed"].sum()),
        }
        for name in QUALITY_FIELDS:
            row[name] = round(100 * served[name].notna().mean(), 0) if len(served) else None
        sinr = served["sinr"].dropna()
        row["sinr_selalu_nol"] = bool(len(sinr) >= 5 and (sinr == 0).all())
        gaps = data_gaps(group)
        row["jeda"] = len(gaps)
        row["menit_jeda"] = float(gaps["menit"].sum()) if len(gaps) else 0.0
        rows.append(row)
    return pd.DataFrame(rows)


def data_gaps(measurements: pd.DataFrame) -> pd.DataFrame:
    """Rentang tanpa data lebih dari GAP_MINUTES, per kos x HP."""
    rows = []
    for (kos, device), group in measurements.groupby(["kos", "device"], sort=True):
        ts = group["ts"].sort_values()
        delta = ts.diff()
        for end, gap in zip(ts[delta > pd.Timedelta(minutes=GAP_MINUTES)], delta[delta > pd.Timedelta(minutes=GAP_MINUTES)]):
            rows.append({"kos": kos, "hp": device, "mulai": end - gap + pd.Timedelta(minutes=1), "selesai": end,
                         "menit": gap.total_seconds() / 60 - 1})
    return pd.DataFrame(rows, columns=["kos", "hp", "mulai", "selesai", "menit"])


def quality_notes(row: pd.Series) -> list[str]:
    """Kalimat awam tentang keterbatasan data satu HP."""
    notes = []
    if row["cell_id"] == 0:
        if row["earfcn"] == 0:
            notes.append("HP ini tidak memberi identitas sel sama sekali, jadi perpindahan sel (handover) tidak bisa dinilai.")
        else:
            notes.append(f"HP ini menyembunyikan identitas sel; perpindahan sel diperkirakan dari frekuensi (EARFCN), "
                         f"yang hanya ada di {row['earfcn']:.0f}% menit.")
    if row["sinr_selalu_nol"]:
        notes.append("SINR selalu persis 0: kemungkinan nilai kosong dari HP, jadi tidak dipakai untuk diagnosis.")
    elif row["sinr"] is not None and row["sinr"] < 100:
        notes.append(f"SINR hanya tersedia di {row['sinr']:.0f}% menit.")
    if row["jeda"]:
        notes.append(f"Data bolong {row['jeda']} kali (total {row['menit_jeda']:.0f} menit): alat ukur berhenti, "
                     "bukan sinyalnya yang hilang.")
    if row["gagal_parse"]:
        notes.append(f"{row['gagal_parse']} pembacaan tidak bisa dibaca dan tidak dihitung.")
    return notes or ["Tidak ada keterbatasan yang terdeteksi."]


# ---------------------------------------------------------------- waktu

def to_local(series: pd.Series, tz: timezone) -> pd.Series:
    """Waktu UTC -> waktu lokal TANPA zona (jam dinding WIB), supaya grafik tidak menggesernya lagi ke zona peramban."""
    return series.dt.tz_convert(tz).dt.tz_localize(None)


def fmt_local(dt: datetime | None, tz: timezone, fmt: str = "%d/%m %H:%M") -> str:
    return "–" if dt is None or pd.isna(dt) else dt.astimezone(tz).strftime(fmt)
