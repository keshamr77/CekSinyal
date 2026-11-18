import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

pd = pytest.importorskip("pandas")

from ceksinyal import db  # noqa: E402
from ceksinyal.config import Config, load_config  # noqa: E402
from ceksinyal.dashboard import data  # noqa: E402
from ceksinyal.dashboard.data import Categories  # noqa: E402
from ceksinyal.diagnosis import engine  # noqa: E402
from ceksinyal.uploader import sheets  # noqa: E402
from ceksinyal.uploader.sheets import Uploader  # noqa: E402
from tests.dashboard_helpers import END, SERIAL_A, SERIAL_B, SERIAL_C, T0, make_db  # noqa: E402

WIB = timezone(timedelta(hours=7))
CATS = Categories()


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    return make_db(tmp_path_factory.mktemp("dash") / "sintetis.db")


@pytest.fixture(scope="module")
def ds(db_path):
    return data.load_sqlite(db_path)


class SheetStore:
    """Backend uploader palsu yang menyimpan tab seperti Sheets: semua sel jadi teks."""

    def __init__(self):
        self.tabs: dict[str, list[list[str]]] = {}

    def append(self, tab, headers, rows):
        sheet = self.tabs.setdefault(tab, [list(headers)])
        sheet.extend([["" if v is None else str(v) for v in row] for row in rows])

    def read(self, tab):
        return self.tabs.get(tab, [])


@pytest.fixture(scope="module")
def sheet_ds(db_path, tmp_path_factory):
    """Data yang sama, lewat jalur sebenarnya: SQLite -> uploader -> (Sheets palsu) -> load_sheets."""
    copy = tmp_path_factory.mktemp("sheets") / "copy.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()
    conn = db.connect(copy)
    store = SheetStore()
    cfg = Config(kos="x")
    cfg.sheets.batch_size = 100000
    Uploader(cfg, conn, store, now=lambda: END + timedelta(hours=1)).upload_pending()
    conn.close()
    return data.load_sheets(store.read), store


# ------------------------------------------------------------ skema & pemuat

def test_schema_and_types(ds):
    assert list(ds.measurements.columns) == data.MEASUREMENT_COLUMNS
    assert list(ds.speedtests.columns) == data.SPEEDTEST_COLUMNS
    assert list(ds.diagnoses.columns) == data.DIAGNOSIS_COLUMNS
    assert list(ds.events.columns) == data.EVENT_COLUMNS
    assert str(ds.measurements["ts"].dt.tz) == "UTC" and str(ds.diagnoses["window_end"].dt.tz) == "UTC"
    assert pd.api.types.is_numeric_dtype(ds.measurements["rsrp"])
    assert ds.kos_names() == ["Garden House", "Kos Melati"]


def test_sqlite_is_aggregated_per_minute(ds):
    m = ds.measurements
    tsel = m[m["operator"] == "Telkomsel"]
    assert len(tsel) == 120 and (tsel["n_samples"] == 6).all()
    first = tsel.iloc[0]
    assert (first["ts"], first["rsrp"], first["device"]) == (pd.Timestamp(T0), -90.0, SERIAL_A)
    lost = tsel[tsel["ts"] == pd.Timestamp(T0 + timedelta(minutes=105))].iloc[0]
    assert lost["n_no_service"] == 6 and pd.isna(lost["rsrp"]) and lost["rat"] == "NONE"
    assert len(m[m["operator"] == "Tri"]) == 100  # jeda 20 menit tidak diisi


def test_sqlite_opened_read_only_and_old_schema(tmp_path):
    path = tmp_path / "lama.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE measurements (id INTEGER PRIMARY KEY, ts TEXT, kos TEXT, device_serial TEXT, operator TEXT, "
        "rat TEXT, rsrp INTEGER, rsrq INTEGER, sinr REAL, rssi INTEGER, pci INTEGER, cell_id INTEGER, earfcn INTEGER, "
        "band INTEGER, raw_ok INTEGER, uploaded INTEGER);"
        "CREATE TABLE diagnoses (id INTEGER PRIMARY KEY, ts TEXT, kos TEXT, operator TEXT, window_start TEXT, "
        "window_end TEXT, n_samples INTEGER, median_rsrp REAL, median_sinr REAL, median_download REAL, "
        "cell_changes INTEGER, result TEXT, advice TEXT, uploaded INTEGER);"
        "INSERT INTO measurements VALUES (1, '2026-11-15T08:00:05+00:00', 'k', 'S', 'XL', 'LTE', -95, -11, 5.5, "
        "NULL, NULL, NULL, 1850, 3, 1, 0);"
        "INSERT INTO diagnoses VALUES (1, 't', 'k', 'XL', '2026-11-15T08:00:00+00:00', '2026-11-15T08:10:00+00:00', "
        "60, -95, 5.5, NULL, NULL, 'Normal', 'ok', 0);"
    )
    old.commit()
    old.close()
    before = path.read_bytes()
    loaded = data.load_sqlite(path)
    assert len(loaded.measurements) == 1 and loaded.diagnoses.iloc[0]["result"] == "Normal"
    assert pd.isna(loaded.diagnoses.iloc[0]["detail"]) and loaded.events.empty and loaded.speedtests.empty
    assert path.read_bytes() == before  # tidak ada migrasi/penulisan
    with pytest.raises(FileNotFoundError):
        data.load_sqlite(tmp_path / "tidak-ada.db")


def test_sheets_and_sqlite_give_identical_frames(ds, sheet_ds):
    from_sheets, store = sheet_ds
    assert set(store.tabs) == {"measurements", "speedtests", "diagnoses", "events"}
    for name in ("measurements", "speedtests", "diagnoses", "events"):
        a, b = getattr(ds, name), getattr(from_sheets, name)
        assert list(a.columns) == list(b.columns), name
        key = [c for c in ("ts", "window_start", "kos", "operator", "device", "method") if c in a.columns]
        a, b = (x.sort_values(key, kind="stable").reset_index(drop=True) for x in (a, b))
        pd.testing.assert_frame_equal(a, b, check_dtype=False, obj=name)


def test_sheets_duplicates_and_missing_tabs():
    header = sheets.DIAGNOSIS_HEADERS
    row = ["1", "t", "kos-a", "XL", "2026-11-15T08:00:00+00:00", "2026-11-15T08:10:00+00:00", "60", "-95", "5", "",
           "", "Interferensi", "saran lama", ""]
    rerun = list(row)
    rerun[0], rerun[11], rerun[12] = "7", "Normal", "saran baru"
    minute = dict(zip(sheets.MINUTE_HEADERS, ["2026-11-15T08:00:00+00:00", "kos-a", "S1", "XL", "6", "0", "0", "LTE",
                                              "-95.0", "-97", "-11.0", "5.0", "", "", "1850", "3"]))
    tabs = {
        "diagnoses": [header, row, rerun],                      # diagnose --rerun: baris lama + baru
        "measurements": [sheets.MINUTE_HEADERS, list(minute.values()), list(minute.values()), [""] * 16],  # batch ganda
    }
    loaded = data.load_sheets(lambda tab: tabs.get(tab, []))
    assert len(loaded.diagnoses) == 1 and loaded.diagnoses.iloc[0]["result"] == "Normal"
    assert len(loaded.measurements) == 1 and loaded.measurements.iloc[0]["rsrp"] == -95.0
    assert pd.isna(loaded.measurements.iloc[0]["cell_id"])
    assert loaded.events.empty and loaded.speedtests.empty and list(loaded.events.columns) == data.EVENT_COLUMNS


def test_sheets_raw_measurements_are_aggregated():
    rows = [sheets.RAW_MEASUREMENT_HEADERS]
    for i, rsrp in enumerate((-90, -100)):
        rows.append([str(i + 1), f"2026-11-15T08:00:{i * 10:02d}+00:00", "kos-a", "S1", "XL", "LTE", str(rsrp), "-10",
                     "5.0", "", "", "", "1850", "3", "1"])
    loaded = data.load_sheets(lambda tab: rows if tab == "measurements" else [])
    assert len(loaded.measurements) == 1
    r = loaded.measurements.iloc[0]
    assert (r["n_samples"], r["rsrp"], r["rsrp_min"], r["earfcn"]) == (2, -95.0, -100, 1850)


def test_events_exclude_daily_marker(ds):
    assert set(ds.events["type"]) == {"signal_lost", "diagnosis_changed", "collector_error"}


def test_filter(ds):
    part = ds.filter(kos=["Garden House"], operators=["Tri"], start=T0 + timedelta(minutes=30), end=T0 + timedelta(minutes=80))
    assert set(part.measurements["operator"]) == {"Tri"} and len(part.measurements) == 31  # 30..49 dan 70..80
    assert set(part.diagnoses["operator"]) == {"Tri"} and set(part.speedtests["kos"]) == {"Garden House"}
    assert part.diagnoses["window_end"].min() >= pd.Timestamp(T0 + timedelta(minutes=30))


# ------------------------------------------------------------ kategori

@pytest.mark.parametrize("metric,value,expected", [
    ("rsrp", -85, "Baik"), ("rsrp", -100, "Baik"), ("rsrp", -100.1, "Cukup"), ("rsrp", -110, "Cukup"),
    ("rsrp", -110.1, "Buruk"), ("rsrq", -10, "Baik"), ("rsrq", -12, "Cukup"), ("rsrq", -15.5, "Buruk"),
    ("sinr", 10, "Baik"), ("sinr", 0, "Cukup"), ("sinr", -0.1, "Buruk"),
    ("download", 12, "Baik"), ("download", 2, "Cukup"), ("download", 1.9, "Buruk"),
    ("rsrp", None, "Tidak ada data"), ("sinr", float("nan"), "Tidak ada data"),
])
def test_categorize(metric, value, expected):
    assert data.categorize(value, metric, CATS) == expected


def test_categories_follow_diagnosis_thresholds(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("diagnosis:\n  rsrp_weak_dbm: -105\n  sinr_interference_db: 3\n"
                 "dashboard:\n  rsrp_good_dbm: -95\n  sinr_good_db: 13\n  download_poor_mbps: 1\n", encoding="utf-8")
    cats = Categories.from_config(load_config(p))
    assert (cats.rsrp_poor, cats.rsrp_good, cats.sinr_poor, cats.sinr_good, cats.download_poor) == (-105, -95, 3, 13, 1)
    assert data.categorize(-107, "rsrp", cats) == "Buruk" and data.categorize(-107, "rsrp", CATS) == "Cukup"
    # batas "baik" tidak boleh di bawah batas "buruk"
    p.write_text("diagnosis:\n  rsrp_weak_dbm: -90\n", encoding="utf-8")
    cats = Categories.from_config(load_config(p))
    assert cats.rsrp_good == cats.rsrp_poor == -90


def test_dashboard_config(tmp_path):
    from ceksinyal.config import ConfigError
    p = tmp_path / "c.yaml"
    p.write_text("dashboard:\n  mode: SQLite\n  public: false\n  stale_minutes: 30\n"
                 "  kos_aliases:\n    Garden House: Kos A\n", encoding="utf-8")
    cfg = load_config(p)
    assert (cfg.dashboard.mode, cfg.dashboard.public, cfg.dashboard.stale_minutes) == ("sqlite", False, 30)
    assert cfg.dashboard.kos_aliases == {"Garden House": "Kos A"}
    assert Config().dashboard.public is True  # privasi adalah bawaan
    p.write_text("dashboard:\n  mode: excel\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


# ------------------------------------------------------------ privasi

def test_alias_map_public_always_aliases():
    names = ["Garden House", "Kos Melati", "Wisma C"]
    assert data.kos_alias_map(names, {"Garden House": "Kos A"}, public=True) == {
        "Garden House": "Kos A", "Kos Melati": "Kos 1", "Wisma C": "Kos 2"}
    assert data.kos_alias_map(names, {"Garden House": "Kos A"}, public=False) == {
        "Garden House": "Kos A", "Kos Melati": "Kos Melati", "Wisma C": "Wisma C"}
    # alias otomatis tidak menabrak alias yang diberikan
    assert data.kos_alias_map(["x", "y"], {"x": "Kos 1"}, public=True) == {"x": "Kos 1", "y": "Kos 2"}


def test_apply_privacy_hides_names_serials_and_paths(ds):
    public = data.apply_privacy(ds, {"Garden House": "Kos A"}, public=True)
    assert public.kos_names() == ["Kos 1", "Kos A"]
    assert set(public.measurements["device"]) == {"HP 1", "HP 2", "HP 3"}
    everything = " ".join(
        df.to_csv() for df in (public.measurements, public.speedtests, public.diagnoses, public.events))
    assert public.speedtests["error"].dropna().iloc[0].startswith("IP sumber (IP) tidak ada")
    for secret in (SERIAL_A, SERIAL_B, SERIAL_C, "Garden House", "Kos Melati", "budi", "C:\\Users", "10.0.0.5"):
        assert secret not in everything, secret
    collector = public.events[public.events["type"] == "collector_error"].iloc[0]
    assert collector["detail"] == "HP terputus dari laptop" and collector["kos"] == "Kos A"
    assert ds.kos_names() == ["Garden House", "Kos Melati"]  # dataset asal tidak berubah


def test_private_mode_keeps_kos_but_never_serials(ds):
    private = data.apply_privacy(ds, {}, public=False)
    assert private.kos_names() == ["Garden House", "Kos Melati"]
    assert SERIAL_A not in private.measurements.to_csv() and SERIAL_B not in private.events.to_csv()


def test_device_labels_are_stable(ds):
    labels = data.device_label_map(ds.measurements)
    assert labels == {SERIAL_A: "HP 1", SERIAL_B: "HP 2", SERIAL_C: "HP 3"}
    assert data.device_label_map(ds.measurements.iloc[::-1]) == labels


# ------------------------------------------------------------ ringkasan

def test_summary_cards(ds):
    cards = {(c.kos, c.operator): c for c in data.summary_cards(ds, CATS, now=END + timedelta(minutes=3))}
    assert set(cards) == {("Garden House", "Telkomsel"), ("Garden House", "Tri"), ("Kos Melati", "XL")}
    tsel = cards[("Garden House", "Telkomsel")]
    assert tsel.medians == {"rsrp": -90.0, "rsrq": -10.0, "sinr": 12.0}
    assert tsel.categories == {"rsrp": "Baik", "rsrq": "Baik", "sinr": "Baik"}
    assert tsel.diagnosis == "Normal" and tsel.advice and not tsel.stale and tsel.minutes_ago < 5
    assert (tsel.download, tsel.download_category) == (27.0, "Baik")
    assert round(tsel.pct_no_service, 1) == 8.3
    tri = cards[("Garden House", "Tri")]
    assert tri.medians["rsrp"] == -109.0 and tri.categories == {"rsrp": "Cukup", "rsrq": "Cukup", "sinr": "Cukup"}
    assert (tri.download, tri.download_category) == (1.5, "Buruk")
    xl = cards[("Kos Melati", "XL")]
    assert xl.diagnosis == "Interferensi" and xl.categories["sinr"] == "Buruk" and xl.download is None
    assert xl.download_category == "Tidak ada data"


def test_summary_stale_warning_and_old_data(ds):
    cards = data.summary_cards(ds, CATS, now=END + timedelta(minutes=17), stale_minutes=15)
    assert all(c.stale for c in cards)
    old = data.summary_cards(ds, CATS, now=END + timedelta(days=3))
    assert old[0].stale and old[0].medians["rsrp"] == -90.0  # median tetap dari 24 jam sebelum data terakhir
    assert data.format_ago(old[0].minutes_ago) == "3 hari lalu"


def test_summary_sinr_always_zero_is_no_data(tmp_path):
    conn = db.connect(tmp_path / "z.db")
    for i in range(12):
        db.insert_measurement(conn, {"ts": db.iso(T0 + timedelta(seconds=10 * i)), "kos": "k", "device_serial": "S",
                                     "operator": "Tri", "rat": "LTE", "rsrp": -82, "sinr": 0.0, "raw_ok": 1})
    conn.close()
    card = data.summary_cards(data.load_sqlite(tmp_path / "z.db"), CATS, now=T0)[0]
    assert card.medians["sinr"] is None and card.categories["sinr"] == "Tidak ada data"


@pytest.mark.parametrize("minutes,text", [(0.5, "baru saja"), (12, "12 menit lalu"), (180, "3 jam lalu"), (5000, "3 hari lalu")])
def test_format_ago(minutes, text):
    assert data.format_ago(minutes) == text


# ------------------------------------------------------------ linimasa

def test_problem_periods_are_merged(ds):
    periods = data.problem_periods(ds.diagnoses)
    got = {(r.kos, r.operator, r.result): (r.start, r.end, r.windows) for r in periods.itertuples()}
    assert got[("Kos Melati", "XL", "Interferensi")] == (pd.Timestamp(T0), pd.Timestamp(END), 12)
    assert got[("Garden House", "Telkomsel", "Sinyal putus")][2] == 1
    weak = got[("Garden House", "Tri", "Cakupan lemah")]
    assert weak[0] == pd.Timestamp(T0) and weak[2] == 5
    assert "Normal" not in set(periods["result"]) and "Data kurang" not in set(periods["result"])


def test_downsample_keeps_gaps_and_limits_points(ds):
    full = data.downsample(ds.measurements, max_points=600)
    tri = full[full["operator"] == "Tri"]
    assert len(tri) == 101 and tri["rsrp"].isna().sum() == 1  # satu baris kosong = garis terputus di jeda
    small = data.downsample(ds.measurements, max_points=30)
    tsel = small[small["operator"] == "Telkomsel"]
    assert len(tsel) <= 31 and tsel["rsrp"].dropna().iloc[0] == -90.0


# ------------------------------------------------------------ perbandingan

def test_category_shares(ds):
    shares = data.category_shares(ds.measurements, "rsrp", CATS)
    tri = shares[shares["operator"] == "Tri"].set_index("kategori")["persen"].to_dict()
    assert tri == {"Baik": 0.0, "Cukup": 50.0, "Buruk": 50.0}
    for _, group in shares.groupby(["kos", "operator"]):
        assert abs(group["persen"].sum() - 100) < 0.2
    assert data.category_shares(ds.measurements, "sinr", CATS).query("operator == 'XL' and kategori == 'Buruk'")["persen"].iloc[0] == 100


def test_comparison_table(ds):
    table = data.comparison_table(ds, CATS).set_index(["kos", "operator"])
    tri = table.loc[("Garden House", "Tri")]
    assert (tri["median_rsrp"], tri["pct_rsrp_buruk"], tri["pct_rsrp_tidak_baik"]) == (-109.0, 50.0, 100.0)
    assert tri["diagnosis_dominan"] in ("Cakupan lemah", "Normal", "Sel padat")
    tsel = table.loc[("Garden House", "Telkomsel")]
    assert round(tsel["pct_tanpa_layanan"], 1) == 8.3 and tsel["median_unduh"] == 23.5
    assert table.loc[("Kos Melati", "XL")]["pct_jendela_bermasalah"] == 100.0


def test_recommendation_matches_engine(ds, db_path):
    recs = {r.kos: r for r in data.recommendations(ds)}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for kos in ("Garden House", "Kos Melati"):
            assert recs[kos].best == engine.recommend(conn, kos).best
    finally:
        conn.close()
    assert recs["Garden House"].best == "Telkomsel" and "jendela Normal" in recs["Garden House"].reason
    assert recs["Kos Melati"].best is None and "minimal dua operator" in recs["Kos Melati"].reason


def test_recommendation_tie_uses_rsrp():
    def frame(rows, table):
        return data._frame(rows, table)

    diag = [{"window_start": "2026-11-15T08:00:00+00:00", "window_end": "2026-11-15T08:10:00+00:00", "kos": "k",
             "operator": op, "result": "Normal"} for op in ("A", "B")]
    meas = [{"ts_minute": "2026-11-15T08:00:00+00:00", "kos": "k", "device_serial": op, "operator": op,
             "n_samples": 6, "rsrp_avg": rsrp} for op, rsrp in (("A", -95), ("B", -85))]
    ds = data.Dataset(frame(meas, "measurements"), frame([], "speedtests"), frame(diag, "diagnoses"), frame([], "events"))
    rec = data.recommendations(ds)[0]
    assert rec.best == "B" and "median RSRP" in rec.reason


# ------------------------------------------------------------ kualitas data

def test_data_quality(ds):
    labelled = data.apply_privacy(ds, {}, public=False)
    q = data.data_quality(labelled.measurements).set_index("hp")
    xiaomi = q.loc["HP 2"]  # Tri: tanpa pci/cell_id, ada jeda
    assert (xiaomi["cell_id"], xiaomi["pci"], xiaomi["earfcn"], xiaomi["rsrp"]) == (0, 0, 100, 100)
    assert (xiaomi["jeda"], xiaomi["menit_jeda"]) == (1, 20)
    notes = " ".join(data.quality_notes(xiaomi))
    assert "menyembunyikan identitas sel" in notes and "Data bolong 1 kali (total 20 menit)" in notes
    good = q.loc["HP 1"]
    assert good["cell_id"] == 100 and good["jeda"] == 0  # menit tanpa layanan bukan "data bolong"
    assert data.quality_notes(good) == ["Tidak ada keterbatasan yang terdeteksi."]
    gaps = data.data_gaps(labelled.measurements)
    assert len(gaps) == 1 and gaps.iloc[0]["hp"] == "HP 2"
    assert gaps.iloc[0]["mulai"] == pd.Timestamp(T0 + timedelta(minutes=50))
    assert gaps.iloc[0]["selesai"] == pd.Timestamp(T0 + timedelta(minutes=70))


# ------------------------------------------------------------ waktu

def test_wib_conversion(ds):
    local = data.to_local(ds.measurements["ts"], WIB)
    assert local.dt.tz is None and local.iloc[0] == pd.Timestamp("2026-11-15 15:00:00")
    assert data.fmt_local(T0, WIB) == "15/11 15:00" and data.fmt_local(None, WIB) == "–"
    late = datetime(2026, 11, 15, 18, 30, tzinfo=timezone.utc)  # lewat tengah malam WIB
    assert data.fmt_local(late, WIB, "%Y-%m-%d %H:%M") == "2026-11-16 01:30"


def test_empty_dataset_functions_do_not_crash():
    empty = data.load_sheets(lambda tab: [])
    assert empty.empty and data.summary_cards(empty, CATS, now=T0) == []
    assert data.problem_periods(empty.diagnoses).empty and data.comparison_table(empty, CATS).empty
    assert data.recommendations(empty) == [] and data.data_quality(empty.measurements).empty
    assert data.category_shares(empty.measurements, "rsrp", CATS).empty
    assert data.downsample(empty.measurements).empty and data.data_gaps(empty.measurements).empty
    assert data.apply_privacy(empty, {}, True).empty


def test_sheets_decimal_comma_text_is_number():
    # Spreadsheet ber-locale Indonesia (in_ID) yang dibaca sebagai teks berformat: "25,68" harus tetap angka.
    header = ["id", "ts", "kos", "operator", "method", "source_ip", "ping_ms", "download_mbps", "upload_mbps", "ok",
              "error"]
    row = ["1", "2026-11-18T05:14:42+00:00", "kos-a", "Telkomsel", "light", "10.0.0.2", "31,6", "25,68", "", "1", ""]
    loaded = data.load_sheets(lambda tab: [header, row] if tab == "speedtests" else [])
    r = loaded.speedtests.iloc[0]
    assert (r["ping_ms"], r["download_mbps"], r["ok"]) == (31.6, 25.68, 1)
    assert pd.isna(r["upload_mbps"])
